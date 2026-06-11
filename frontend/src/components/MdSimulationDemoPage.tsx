import {
  Activity,
  ArrowLeft,
  Atom,
  CheckCircle2,
  Gauge,
  Layers3,
  Loader2,
  Play,
  RotateCw,
  Timer,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { calculateMdDemoAtomDistance, fetchMdDemoDefaults, runMdDemo } from "../services/api";
import type {
  MdDemoAtomDistanceResponse,
  MdDemoAtomSelection,
  MdDemoDefaultsResponse,
  MdDemoRunRequest,
  MdDemoRunResponse,
  MdDemoSeries,
  MdDemoTrajectoryPoint,
} from "../types";
import { cn } from "../lib/utils";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";

type MdSimulationDemoPageProps = {
  onBackHome: () => void;
};

type ProgressStep = {
  label: string;
  detail: string;
  threshold: number;
};

type ProgressKeyframe = {
  time: number;
  value: number;
};

const DEFAULT_REQUEST: MdDemoRunRequest = {
  smiles: "*C(=C(*)C(F)(F)F)c1ccc(CCCC)cc1",
  temperature: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod",
};

const FINAL_RESULT_DELAY_MS = 2000;

const PROGRESS_KEYFRAMES: ProgressKeyframe[] = [
  { time: 0, value: 1 },
  { time: 280, value: 6 },
  { time: 640, value: 8 },
  { time: 1050, value: 8 },
  { time: 1500, value: 24 },
  { time: 1850, value: 28 },
  { time: 2350, value: 28 },
  { time: 2920, value: 44 },
  { time: 3320, value: 52 },
  { time: 3920, value: 68 },
  { time: 4320, value: 68 },
  { time: 4860, value: 86 },
  { time: 5220, value: 93 },
  { time: 5600, value: 99 },
];

const PROGRESS_STEPS: ProgressStep[] = [
  { label: "任务排队", detail: "检查输入结构与默认参数", threshold: 8 },
  { label: "体系建模", detail: "构建聚合物链与初始盒子", threshold: 28 },
  { label: "EQ1 初始平衡", detail: "短程松弛和初始轨迹登记", threshold: 48 },
  { label: "EQ2 密度收敛", detail: "压力耦合和盒子尺寸稳定", threshold: 68 },
  { label: "EQ3 生产采样", detail: "读取现有生产采样曲线", threshold: 88 },
  { label: "结果汇总", detail: "提取轨迹预览和距离曲线", threshold: 100 },
];

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function formatNumber(value: number, fractionDigits = 2) {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

function progressStep(progress: number) {
  return PROGRESS_STEPS.find((step) => progress <= step.threshold) ?? PROGRESS_STEPS[PROGRESS_STEPS.length - 1];
}

function scriptedProgress(elapsed: number) {
  const firstFrame = PROGRESS_KEYFRAMES[0];
  if (elapsed <= firstFrame.time) {
    return firstFrame.value;
  }

  for (let index = 1; index < PROGRESS_KEYFRAMES.length; index += 1) {
    const previous = PROGRESS_KEYFRAMES[index - 1];
    const next = PROGRESS_KEYFRAMES[index];
    if (elapsed <= next.time) {
      if (previous.value === next.value) {
        return next.value;
      }
      const ratio = (elapsed - previous.time) / (next.time - previous.time);
      const easedRatio = 1 - Math.pow(1 - clamp(ratio, 0, 1), 2);
      return Math.round(previous.value + (next.value - previous.value) * easedRatio);
    }
  }

  return 99;
}

function parseNumberInput(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function MdSimulationDemoPage({ onBackHome }: MdSimulationDemoPageProps) {
  const [request, setRequest] = useState<MdDemoRunRequest>(DEFAULT_REQUEST);
  const [defaults, setDefaults] = useState<MdDemoDefaultsResponse | null>(null);
  const [result, setResult] = useState<MdDemoRunResponse | null>(null);
  const [pendingResult, setPendingResult] = useState<MdDemoRunResponse | null>(null);
  const [progress, setProgress] = useState(0);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeStage, setActiveStage] = useState("eq3");
  const [activeThermoKey, setActiveThermoKey] = useState("temp");
  const [selectedAtoms, setSelectedAtoms] = useState<MdDemoAtomSelection[]>([]);
  const [atomDistance, setAtomDistance] = useState<MdDemoAtomDistanceResponse | null>(null);
  const [atomDistanceError, setAtomDistanceError] = useState<string | null>(null);
  const [atomDistanceLoading, setAtomDistanceLoading] = useState(false);
  const timerRef = useRef<number | null>(null);
  const finalRevealTimerRef = useRef<number | null>(null);
  const atomDistanceRequestIdRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    fetchMdDemoDefaults()
      .then((data) => {
        if (cancelled) {
          return;
        }
        setDefaults(data);
        setRequest(data.default_request);
      })
      .catch((fetchError) => {
        if (!cancelled) {
          setError(fetchError instanceof Error ? fetchError.message : "无法加载 MD 模拟默认数据。");
        }
      });

    return () => {
      cancelled = true;
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
      }
      if (finalRevealTimerRef.current !== null) {
        window.clearTimeout(finalRevealTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!isRunning || !pendingResult || progress < 99 || finalRevealTimerRef.current !== null) {
      return;
    }

    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }

    finalRevealTimerRef.current = window.setTimeout(() => {
      setProgress(100);
      setResult(pendingResult);
      setPendingResult(null);
      setIsRunning(false);
      setActiveStage("eq3");
      finalRevealTimerRef.current = null;
    }, FINAL_RESULT_DELAY_MS);
  }, [isRunning, pendingResult, progress]);

  const currentStep = progressStep(progress);
  const displayedResult = result;
  const progressLabel = displayedResult && !isRunning ? "100/100" : `${progress}/100`;
  const progressBarValue = displayedResult && !isRunning ? 100 : progress;
  const headerDensityValue = displayedResult ? `${displayedResult.summary.final_density_g_cm3.toFixed(3)} g/cm3` : "--";
  const headerTrajectoryValue = displayedResult ? formatNumber(displayedResult.summary.n_frames, 0) : "--";
  const stageSummaries = displayedResult?.stages ?? defaults?.available_stages ?? [];
  const selectedStage = stageSummaries.find((stage) => stage.stage_id === activeStage) ?? stageSummaries[stageSummaries.length - 1];
  const activeThermo =
    displayedResult?.thermo_series.find((series) => series.key === activeThermoKey) ??
    displayedResult?.thermo_series[0] ??
    null;
  const canRun = request.smiles.trim().length > 0 && !isRunning;

  function updateRequest(partial: Partial<MdDemoRunRequest>) {
    setRequest((current) => ({ ...current, ...partial }));
  }

  async function handleRun() {
    const normalizedSmiles = request.smiles.trim();
    if (!normalizedSmiles) {
      setError("请先输入 SMILES。");
      return;
    }

    const requestBody = { ...request, smiles: normalizedSmiles };
    setRequest(requestBody);
    setError(null);
    setResult(null);
    setPendingResult(null);
    setSelectedAtoms([]);
    setAtomDistance(null);
    setAtomDistanceError(null);
    setAtomDistanceLoading(false);
    atomDistanceRequestIdRef.current += 1;
    setProgress(0);
    setIsRunning(true);

    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
    }
    if (finalRevealTimerRef.current !== null) {
      window.clearTimeout(finalRevealTimerRef.current);
      finalRevealTimerRef.current = null;
    }

    const startedAt = window.performance.now();
    timerRef.current = window.setInterval(() => {
      const elapsed = window.performance.now() - startedAt;
      const nextProgress = scriptedProgress(elapsed);
      setProgress(nextProgress);
      if (nextProgress >= 99 && timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }, 140);

    try {
      const response = await runMdDemo(requestBody);
      setPendingResult(response);
    } catch (runError) {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      if (finalRevealTimerRef.current !== null) {
        window.clearTimeout(finalRevealTimerRef.current);
        finalRevealTimerRef.current = null;
      }
      setIsRunning(false);
      setProgress(0);
      setError(runError instanceof Error ? runError.message : "MD 模拟运行失败。");
    }
  }

  function handleAtomSelect(atom: MdDemoAtomSelection) {
    setAtomDistance(null);
    setAtomDistanceError(null);
    setSelectedAtoms((current) => nextAtomSelection(current, atom));
  }

  function handleClearAtomSelection() {
    atomDistanceRequestIdRef.current += 1;
    setSelectedAtoms([]);
    setAtomDistance(null);
    setAtomDistanceError(null);
    setAtomDistanceLoading(false);
  }

  async function handleCalculateDistance() {
    if (selectedAtoms.length < 2) {
      setAtomDistanceError("请先在左侧 3D 图中点击选择两个原子。");
      setAtomDistance(null);
      return;
    }

    const resolvedAtoms = [selectedAtoms[0], selectedAtoms[1]] as const;
    const requestId = ++atomDistanceRequestIdRef.current;
    setAtomDistance(null);
    setAtomDistanceError(null);
    setAtomDistanceLoading(true);

    try {
      const response = await calculateMdDemoAtomDistance({
        atom_id_1: resolvedAtoms[0].atom_id,
        atom_id_2: resolvedAtoms[1].atom_id,
        use_pbc: true,
      });
      if (requestId === atomDistanceRequestIdRef.current) {
        setAtomDistance(response);
      }
    } catch (distanceError) {
      if (requestId === atomDistanceRequestIdRef.current) {
        setAtomDistanceError(distanceError instanceof Error ? distanceError.message : "原子对距离曲线计算失败。");
      }
    } finally {
      if (requestId === atomDistanceRequestIdRef.current) {
        setAtomDistanceLoading(false);
      }
    }
  }

  return (
    <div className="relative -mx-4 -my-5 min-h-[calc(100vh-0px)] overflow-x-clip bg-[#f3f8fb] text-slate-950 md:-mx-8 md:-my-8">
      <header className="sticky top-0 z-20 border-b border-slate-200/80 bg-white/92 px-4 py-4 shadow-sm backdrop-blur md:px-8">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <Button type="button" variant="outline" className="h-10 rounded-xl px-3" onClick={onBackHome}>
              <ArrowLeft className="mr-2 h-4 w-4" />
              Home
            </Button>
            <div>
              <h1 className="font-heading mt-2 text-2xl font-semibold tracking-tight md:text-3xl">
                分子动力学模拟
              </h1>
            </div>
          </div>
          <div className="grid gap-2 text-sm text-slate-600 sm:grid-cols-2 lg:min-w-[380px]">
            <HeaderMetric label="最终密度" value={headerDensityValue} />
            <HeaderMetric label="轨迹帧数" value={headerTrajectoryValue} />
          </div>
        </div>
      </header>

      <main className="px-4 py-5 md:px-6 md:py-6">
        <div className="grid items-stretch gap-5 xl:grid-cols-[minmax(360px,0.36fr)_minmax(0,0.64fr)]">
          <section className="flex h-full flex-col gap-5">
            <Panel>
              <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
                <div>
                  <h2 className="font-heading text-lg font-semibold">输入结构与参数</h2>
                </div>
                <Atom className="h-5 w-5 text-cyan-600" />
              </div>
              <div className="space-y-4 p-5">
                <label className="block space-y-2">
                  <span className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-500">SMILES</span>
                  <Textarea
                    value={request.smiles}
                    onChange={(event) => updateRequest({ smiles: event.target.value })}
                    spellCheck={false}
                    className="min-h-[98px] resize-none rounded-2xl border-slate-200 bg-white font-mono-ui text-sm leading-6"
                    placeholder="例如：*C(=C(*)C(F)(F)F)c1ccc(CCCC)cc1"
                  />
                </label>

                <div className="grid gap-3 sm:grid-cols-2">
                  <NumberField label="温度 (K)" value={request.temperature} onChange={(value) => updateRequest({ temperature: value })} />
                  <NumberField label="压力 (atm)" value={request.pressure} onChange={(value) => updateRequest({ pressure: value })} />
                  <NumberField label="原子数" value={request.n_atom} onChange={(value) => updateRequest({ n_atom: Math.round(value) })} />
                  <NumberField label="链数" value={request.n_chain} onChange={(value) => updateRequest({ n_chain: Math.round(value) })} />
                </div>

                <label className="block space-y-2">
                  <span className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-500">Forcefield</span>
                  <Input
                    value={request.forcefield}
                    onChange={(event) => updateRequest({ forcefield: event.target.value })}
                    className="rounded-2xl border-slate-200 bg-white"
                  />
                </label>

                <Button
                  type="button"
                  className="h-12 w-full rounded-2xl bg-cyan-700 text-white shadow-[0_18px_38px_rgba(8,145,178,0.28)] hover:bg-cyan-600"
                  onClick={handleRun}
                  disabled={!canRun}
                >
                  {isRunning ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                  {isRunning ? "MD 模拟运行中..." : "运行 MD 模拟"}
                </Button>

                {error ? (
                  <div className="rounded-2xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-700">
                    {error}
                  </div>
                ) : null}
              </div>
            </Panel>

            <Panel className="flex-1">
              <div className="p-5">
                <div className="flex items-center justify-between">
                  <h2 className="font-heading text-lg font-semibold">流程进度</h2>
                  <Timer className="h-5 w-5 text-cyan-600" />
                </div>
                <div className="mt-5">
                  <div className="flex items-end justify-between gap-4">
                    <div>
                      <div className="text-sm font-semibold text-slate-900">{isRunning ? currentStep.label : displayedResult ? "结果完成" : "等待运行"}</div>
                      <div className="mt-1 text-sm text-slate-500">{isRunning ? currentStep.detail : displayedResult ? "轨迹数据已载入。" : "输入 SMILES 后启动模拟。"}</div>
                    </div>
                    <div className="font-heading text-3xl font-semibold text-cyan-700">{progressLabel}</div>
                  </div>
                  <div className="mt-4 rounded-2xl border border-slate-100 bg-slate-50 px-4 py-3">
                    <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-cyan-700">
                      <span className={cn("h-2 w-2 rounded-full bg-cyan-600", isRunning ? "animate-pulse" : "")} />
                      {isRunning ? "Running" : displayedResult ? "Complete" : "Ready"}
                    </div>
                    <div className="mt-3 h-2.5 overflow-hidden rounded-full bg-white shadow-inner">
                      <div
                        className="relative h-full overflow-hidden rounded-full bg-cyan-600 transition-[width] duration-700 ease-out"
                        style={{ width: `${progressBarValue}%` }}
                      >
                        {isRunning ? (
                          <span className="absolute inset-y-0 right-0 w-12 animate-pulse bg-gradient-to-r from-transparent via-white/40 to-transparent opacity-80" />
                        ) : null}
                      </div>
                    </div>
                    <div className="mt-2 text-sm leading-6 text-slate-600">
                      {isRunning && progress >= 99
                        ? "结果正在整理，稍后载入输出面板。"
                        : isRunning
                          ? currentStep.detail
                          : displayedResult
                            ? "阶段结果、曲线和最终帧预览已载入。"
                            : "等待输入并启动模拟。"}
                    </div>
                  </div>
                </div>
                <div className="mt-5 space-y-2">
                  {PROGRESS_STEPS.map((step, index) => {
                    const previousThreshold = index === 0 ? 0 : PROGRESS_STEPS[index - 1].threshold;
                    const current = isRunning && progress > previousThreshold && progress <= step.threshold;
                    const complete = (displayedResult && !isRunning) || progress > step.threshold;
                    return (
                      <div
                        key={step.label}
                        className={cn(
                          "flex items-start gap-3 rounded-2xl border px-3 py-2.5 transition",
                          complete
                            ? "border-cyan-100 bg-cyan-50/70"
                            : current
                              ? "border-cyan-200 bg-white shadow-[0_10px_24px_rgba(8,145,178,0.10)]"
                              : "border-slate-100 bg-slate-50/70"
                        )}
                      >
                        <span
                          className={cn(
                            "mt-0.5 flex h-5 w-5 items-center justify-center rounded-full",
                            complete
                              ? "bg-cyan-600 text-white"
                              : current
                                ? "bg-white text-cyan-700 ring-1 ring-cyan-200"
                                : "bg-white text-slate-300 ring-1 ring-slate-200"
                          )}
                        >
                          {complete ? (
                            <CheckCircle2 className="h-3.5 w-3.5" />
                          ) : current ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <RotateCw className="h-3.5 w-3.5" />
                          )}
                        </span>
                        <span>
                          <span className="block text-sm font-semibold text-slate-800">{step.label}</span>
                          <span className="block text-xs leading-5 text-slate-500">{step.detail}</span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </Panel>
          </section>

          <section className="space-y-5">
            {!displayedResult ? (
              <EmptyResultsPanel isRunning={isRunning} progress={progress} currentStep={currentStep} />
            ) : (
              <>
                <div className="grid gap-4 md:grid-cols-3">
                  <SummaryTile icon={<Gauge className="h-5 w-5" />} label="Density" value={`${displayedResult.summary.final_density_g_cm3.toFixed(3)} g/cm3`} detail="eq3 final sample" />
                  <SummaryTile icon={<Activity className="h-5 w-5" />} label="Temperature" value={`${displayedResult.summary.mean_temperature_k.toFixed(1)} K`} detail="last-window mean" />
                  <SummaryTile icon={<Layers3 className="h-5 w-5" />} label="Trajectory" value={`${formatNumber(displayedResult.summary.n_frames, 0)} frames`} detail={`${formatNumber(displayedResult.summary.n_atoms, 0)} atoms`} />
                </div>

                <Panel>
                  <div className="border-b border-slate-100 px-5 py-4">
                    <h2 className="font-heading text-lg font-semibold">阶段结果</h2>
                  </div>
                  <div className="grid gap-3 p-5 lg:grid-cols-3">
                    {displayedResult.stages.map((stage) => (
                      <button
                        key={stage.stage_id}
                        type="button"
                        className={cn(
                          "rounded-2xl border p-4 text-left transition",
                          activeStage === stage.stage_id
                            ? "border-cyan-300 bg-cyan-50 shadow-[0_14px_30px_rgba(8,145,178,0.12)]"
                            : "border-slate-200 bg-white hover:border-cyan-200"
                        )}
                        onClick={() => setActiveStage(stage.stage_id)}
                      >
                        <div className="text-sm font-semibold text-slate-900">{stage.label}</div>
                        <div className="mt-2 text-xs leading-5 text-slate-500">{stage.description}</div>
                        <div className="mt-4 text-xs text-slate-600">
                          <span>{formatNumber(stage.n_frames, 0)} frames</span>
                        </div>
                      </button>
                    ))}
                  </div>
                  {selectedStage ? (
                    <div className="grid gap-3 border-t border-slate-100 px-5 py-4 text-sm text-slate-600 md:grid-cols-4">
                      <StageMetric label="Atoms" value={formatNumber(selectedStage.n_atoms, 0)} />
                      <StageMetric label="Chains" value={formatNumber(selectedStage.n_chains, 0)} />
                      <StageMetric label="dt" value={`${formatNumber(selectedStage.dt_ps, 2)} ps`} />
                      <StageMetric label="Box L" value={`${formatNumber(selectedStage.box.lx, 2)} A`} />
                    </div>
                  ) : null}
                </Panel>

                <div className="grid gap-5 xl:grid-cols-2">
                  <Panel>
                    <ChartHeader title="密度曲线" subtitle="eq3 LAMMPS log sample" />
                    <SeriesChart series={displayedResult.density_series} color="#0891b2" />
                  </Panel>
                  <Panel>
                    <div className="flex flex-col gap-3 border-b border-slate-100 px-5 py-4 md:flex-row md:items-center md:justify-between">
                      <div>
                        <h2 className="font-heading text-lg font-semibold">热力学曲线</h2>
                        <p className="mt-1 text-sm text-slate-500">选择一条热力学序列查看。</p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {displayedResult.thermo_series.map((series) => (
                          <button
                            key={series.key}
                            type="button"
                            className={cn(
                              "rounded-full border px-3 py-1.5 text-xs font-semibold",
                              activeThermo?.key === series.key
                                ? "border-cyan-300 bg-cyan-50 text-cyan-800"
                                : "border-slate-200 bg-white text-slate-500 hover:text-slate-900"
                            )}
                            onClick={() => setActiveThermoKey(series.key)}
                          >
                            {series.label}
                          </button>
                        ))}
                      </div>
                    </div>
                    {activeThermo ? <SeriesChart series={activeThermo} color="#2563eb" /> : null}
                  </Panel>
                </div>

                <div className="grid gap-5 xl:grid-cols-[minmax(0,0.58fr)_minmax(0,0.42fr)]">
                  <Panel>
                    <ChartHeader title="最终帧原子分布" subtitle={`eq3 最终帧 · ${displayedResult.trajectory_preview.sampled_points} 个原子`} />
                    <Trajectory3DPreview
                      points={displayedResult.trajectory_preview.points}
                      selectedAtoms={selectedAtoms}
                      onAtomSelect={handleAtomSelect}
                    />
                  </Panel>
                  <AtomDistancePanel
                    selectedAtoms={selectedAtoms}
                    distance={atomDistance}
                    loading={atomDistanceLoading}
                    error={atomDistanceError}
                    onCalculate={handleCalculateDistance}
                    onClear={handleClearAtomSelection}
                  />
                </div>
              </>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}

function HeaderMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3">
      <div className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-400">{label}</div>
      <div className="mt-1 font-semibold text-slate-900">{value}</div>
    </div>
  );
}

function Panel({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("overflow-hidden rounded-[26px] border border-white/80 bg-white shadow-sm", className)}>{children}</div>;
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return (
    <label className="block space-y-2">
      <span className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-500">{label}</span>
      <Input
        type="number"
        value={value}
        onChange={(event) => onChange(parseNumberInput(event.target.value, value))}
        className="rounded-2xl border-slate-200 bg-white"
      />
    </label>
  );
}

function EmptyResultsPanel({
  isRunning,
  progress,
  currentStep,
}: {
  isRunning: boolean;
  progress: number;
  currentStep: ProgressStep;
}) {
  return (
    <Panel className="min-h-[520px]">
      <div className="flex min-h-[520px] flex-col items-center justify-center px-6 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-[24px] bg-cyan-50 text-cyan-700">
          {isRunning ? <Loader2 className="h-7 w-7 animate-spin" /> : <Activity className="h-7 w-7" />}
        </div>
        <h2 className="font-heading mt-5 text-2xl font-semibold text-slate-950">
          {isRunning ? "正在生成 MD 模拟结果" : "等待 MD 模拟运行"}
        </h2>
        {isRunning ? (
          <p className="mt-3 max-w-xl text-sm leading-7 text-slate-500">
            {`${currentStep.label} ${progress}/100，结果整理完成后会展示阶段摘要、曲线和最终帧预览。`}
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

function SummaryTile({ icon, label, value, detail }: { icon: React.ReactNode; label: string; value: string; detail: string }) {
  return (
    <div className="rounded-[24px] border border-white/80 bg-white p-5 shadow-sm">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-cyan-50 text-cyan-700">{icon}</span>
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-400">{label}</div>
          <div className="font-heading mt-1 text-xl font-semibold text-slate-950">{value}</div>
          <div className="mt-1 text-xs text-slate-500">{detail}</div>
        </div>
      </div>
    </div>
  );
}

function StageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl bg-slate-50 px-4 py-3">
      <div className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-400">{label}</div>
      <div className="mt-1 font-semibold text-slate-900">{value}</div>
    </div>
  );
}

function ChartHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="border-b border-slate-100 px-5 py-4">
      <h2 className="font-heading text-lg font-semibold">{title}</h2>
      <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
    </div>
  );
}

function AtomDistancePanel({
  selectedAtoms,
  distance,
  loading,
  error,
  onCalculate,
  onClear,
}: {
  selectedAtoms: MdDemoAtomSelection[];
  distance: MdDemoAtomDistanceResponse | null;
  loading: boolean;
  error: string | null;
  onCalculate: () => void;
  onClear: () => void;
}) {
  const subtitle = distance
    ? `Atom ${distance.atom_1.atom_id} 与 Atom ${distance.atom_2.atom_id} 的三维距离`
    : "在左侧 3D 图中点击两个原子后计算距离曲线。";

  return (
    <Panel>
      <ChartHeader title="原子对距离曲线" subtitle={subtitle} />
      <div className="space-y-4 p-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <AtomSelectionDisplay label="Atom 1" selectedAtom={selectedAtoms[0]} />
          <AtomSelectionDisplay label="Atom 2" selectedAtom={selectedAtoms[1]} />
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            className="h-10 rounded-xl bg-violet-700 px-4 text-white hover:bg-violet-600"
            disabled={loading || selectedAtoms.length < 2}
            onClick={onCalculate}
          >
            {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
            {loading ? "计算中..." : "计算距离曲线"}
          </Button>
          <Button type="button" variant="outline" className="h-10 rounded-xl px-4" onClick={onClear} disabled={loading && selectedAtoms.length === 0}>
            清空选择
          </Button>
        </div>
        {error ? (
          <div className="rounded-2xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-700">
            {error}
          </div>
        ) : null}
      </div>
      {loading ? (
        <div className="flex h-[260px] items-center justify-center border-t border-slate-100 text-sm text-slate-500">
          <Loader2 className="mr-2 h-5 w-5 animate-spin text-violet-700" />
          正在计算原子对距离曲线
        </div>
      ) : distance ? (
        <>
          <SeriesChart series={distance.series} color="#7c3aed" />
          <div className="flex flex-wrap justify-between gap-3 border-t border-slate-100 px-5 py-3 text-xs text-slate-500">
            <span>{distance.stats.n_frames} sampled frames</span>
            <span>source {distance.stats.source_n_frames} frames</span>
            <span>{distance.stats.use_pbc ? "PBC on" : "PBC off"}</span>
          </div>
        </>
      ) : (
        <div className="flex h-[260px] items-center justify-center border-t border-slate-100 px-6 text-center text-sm leading-6 text-slate-500">
          请先在左侧 3D 原子分布图中依次点击两个不同原子。
        </div>
      )}
    </Panel>
  );
}

function AtomSelectionDisplay({
  label,
  selectedAtom,
}: {
  label: string;
  selectedAtom?: MdDemoAtomSelection;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3">
      <div className="text-xs font-semibold uppercase tracking-[0.12em] text-slate-500">{label}</div>
      <div className="mt-2 font-heading text-2xl font-semibold text-slate-950">
        {selectedAtom ? selectedAtom.atom_id : "--"}
      </div>
      <div className="mt-1 min-h-5 text-xs text-slate-500">
        {selectedAtom ? `Chain ${selectedAtom.chain_id} · Type ${selectedAtom.atom_type}` : "左侧点击选择"}
      </div>
    </div>
  );
}
function SeriesChart({ series, color }: { series: MdDemoSeries; color: string }) {
  const geometry = useMemo(() => buildSeriesGeometry(series), [series]);
  return (
    <div className="p-5">
      <div className="h-[260px] rounded-2xl border border-slate-100 bg-slate-50 p-3">
        <svg viewBox="0 0 640 240" className="h-full w-full" role="img" aria-label={`${series.label} chart`}>
          <g stroke="#dbe4ee" strokeDasharray="4 6" strokeWidth="1">
            <line x1="42" y1="28" x2="42" y2="202" />
            <line x1="42" y1="202" x2="612" y2="202" />
            <line x1="42" y1="72" x2="612" y2="72" />
            <line x1="42" y1="116" x2="612" y2="116" />
            <line x1="42" y1="160" x2="612" y2="160" />
          </g>
          <path d={`${geometry.areaPath} L 612 202 L 42 202 Z`} fill={color} opacity="0.1" />
          <path d={geometry.linePath} fill="none" stroke={color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" />
          {geometry.points.map((point, index) => (index % 12 === 0 ? <circle key={point.x + "-" + point.y} cx={point.x} cy={point.y} r="3" fill="#fff" stroke={color} strokeWidth="2" /> : null))}
        </svg>
      </div>
      <div className="mt-3 flex flex-wrap justify-between gap-3 text-xs text-slate-500">
        <span>
          min {formatNumber(geometry.minValue, 4)} {series.unit}
        </span>
        <span>
          max {formatNumber(geometry.maxValue, 4)} {series.unit}
        </span>
        <span>{series.points.length} points</span>
      </div>
    </div>
  );
}

function buildSeriesGeometry(series: MdDemoSeries) {
  const values = series.points.map((point) => point.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const span = maxValue - minValue || 1;
  const points = series.points.map((point, index) => {
    const x = 42 + (index / Math.max(series.points.length - 1, 1)) * 570;
    const y = 202 - ((point.value - minValue) / span) * 174;
    return { x: roundForSvg(x), y: roundForSvg(y) };
  });
  const linePath = points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
  return { points, linePath, areaPath: linePath, minValue, maxValue };
}

function roundForSvg(value: number) {
  return Math.round(value * 100) / 100;
}

function nextAtomSelection(current: MdDemoAtomSelection[], atom: MdDemoAtomSelection) {
  if (current.some((item) => item.atom_id === atom.atom_id)) {
    return current;
  }
  if (current.length < 2) {
    return [...current, atom];
  }
  return [current[1], atom];
}

function atomSelectionFromPoint(point: MdDemoTrajectoryPoint): MdDemoAtomSelection {
  return {
    atom_id: point.atom_id,
    chain_id: point.chain_id,
    atom_type: point.atom_type,
  };
}

type TrajectoryView = {
  rotationX: number;
  rotationY: number;
  zoom: number;
};

type CanvasSize = {
  width: number;
  height: number;
};

type DragState = {
  pointerId: number;
  startX: number;
  startY: number;
  moved: boolean;
  view: TrajectoryView;
};

type TrajectoryCloud = {
  points: MdDemoTrajectoryPoint[];
  center: { x: number; y: number; z: number };
  radius: number;
};

type ProjectedTrajectoryPoint = {
  point: MdDemoTrajectoryPoint;
  x: number;
  y: number;
  z: number;
  depth: number;
};

const INITIAL_TRAJECTORY_VIEW: TrajectoryView = {
  rotationX: -0.45,
  rotationY: 0.62,
  zoom: 1,
};

function Trajectory3DPreview({
  points,
  selectedAtoms,
  onAtomSelect,
}: {
  points: MdDemoTrajectoryPoint[];
  selectedAtoms: MdDemoAtomSelection[];
  onAtomSelect: (atom: MdDemoAtomSelection) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const dragStateRef = useRef<DragState | null>(null);
  const [view, setView] = useState<TrajectoryView>(INITIAL_TRAJECTORY_VIEW);
  const [canvasSize, setCanvasSize] = useState<CanvasSize>({ width: 0, height: 0 });
  const cloud = useMemo(() => buildTrajectoryCloud(points), [points]);
  const selectedAtomIds = useMemo(() => new Set(selectedAtoms.map((atom) => atom.atom_id)), [selectedAtoms]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const resize = () => {
      const parent = canvas.parentElement;
      if (!parent) {
        return;
      }
      const rect = parent.getBoundingClientRect();
      setCanvasSize({ width: Math.max(1, rect.width), height: Math.max(1, rect.height) });
    };

    resize();
    const observer = new ResizeObserver(resize);
    if (canvas.parentElement) {
      observer.observe(canvas.parentElement);
    }
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    drawTrajectoryCloud(canvasRef.current, cloud, selectedAtomIds, view, canvasSize);
  }, [canvasSize, cloud, selectedAtomIds, view]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const handleCanvasWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const zoomDelta = event.deltaY > 0 ? 0.9 : 1.1;
      setView((current) => ({ ...current, zoom: clamp(current.zoom * zoomDelta, 0.55, 2.7) }));
    };

    canvas.addEventListener("wheel", handleCanvasWheel, { passive: false });
    return () => {
      canvas.removeEventListener("wheel", handleCanvasWheel);
    };
  }, []);

  function handlePointerDown(event: React.PointerEvent<HTMLCanvasElement>) {
    dragStateRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
      view,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function handlePointerMove(event: React.PointerEvent<HTMLCanvasElement>) {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) {
      return;
    }

    const deltaX = event.clientX - dragState.startX;
    const deltaY = event.clientY - dragState.startY;
    if (Math.abs(deltaX) > 3 || Math.abs(deltaY) > 3) {
      dragState.moved = true;
    }
    setView({
      ...dragState.view,
      rotationX: clamp(dragState.view.rotationX + deltaY * 0.008, -Math.PI / 2, Math.PI / 2),
      rotationY: dragState.view.rotationY + deltaX * 0.008,
    });
  }

  function handlePointerUp(event: React.PointerEvent<HTMLCanvasElement>) {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) {
      return;
    }
    dragStateRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    if (dragState.moved) {
      return;
    }

    const rect = event.currentTarget.getBoundingClientRect();
    const point = pickTrajectoryPoint(
      cloud,
      view,
      { width: rect.width, height: rect.height },
      event.clientX - rect.left,
      event.clientY - rect.top
    );
    if (point) {
      onAtomSelect(atomSelectionFromPoint(point));
    }
  }

  return (
    <div className="p-5">
      <div className="relative h-[360px] overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <canvas
          ref={canvasRef}
          className="absolute inset-0 h-full w-full cursor-crosshair touch-none"
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={() => {
            dragStateRef.current = null;
          }}
        />
        {!points.length ? (
          <div className="absolute inset-0 flex items-center justify-center px-6 text-center text-sm text-slate-500">
            暂无轨迹坐标可渲染。
          </div>
        ) : null}
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-500">
        {selectedAtoms.length ? (
          selectedAtoms.map((atom, index) => (
            <span key={atom.atom_id} className="rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-amber-800">
              Atom {index + 1}: {atom.atom_id}
            </span>
          ))
        ) : (
          <span>点击原子后会在右侧显示 Atom 1 / Atom 2 序号。</span>
        )}
      </div>
    </div>
  );
}

function buildTrajectoryCloud(points: MdDemoTrajectoryPoint[]): TrajectoryCloud {
  if (!points.length) {
    return {
      points,
      center: { x: 0, y: 0, z: 0 },
      radius: 1,
    };
  }

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let minZ = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  let maxZ = Number.NEGATIVE_INFINITY;

  points.forEach((point) => {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    minZ = Math.min(minZ, point.z);
    maxX = Math.max(maxX, point.x);
    maxY = Math.max(maxY, point.y);
    maxZ = Math.max(maxZ, point.z);
  });

  const center = {
    x: (minX + maxX) / 2,
    y: (minY + maxY) / 2,
    z: (minZ + maxZ) / 2,
  };
  const radius = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 1) / 2;
  return { points, center, radius };
}

function drawTrajectoryCloud(
  canvas: HTMLCanvasElement | null,
  cloud: TrajectoryCloud,
  selectedAtomIds: Set<number>,
  view: TrajectoryView,
  size: CanvasSize
) {
  if (!canvas || size.width <= 0 || size.height <= 0) {
    return;
  }

  const context = canvas.getContext("2d");
  if (!context) {
    return;
  }

  const pixelRatio = window.devicePixelRatio || 1;
  canvas.width = Math.round(size.width * pixelRatio);
  canvas.height = Math.round(size.height * pixelRatio);
  canvas.style.width = `${size.width}px`;
  canvas.style.height = `${size.height}px`;
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, size.width, size.height);
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, size.width, size.height);

  const projectedPoints = projectTrajectoryPoints(cloud, view, size).sort((left, right) => left.z - right.z);
  projectedPoints.forEach((projected) => {
    const selected = selectedAtomIds.has(projected.point.atom_id);
    const radius = selected ? 5.2 : 1.35 + projected.depth * 0.8;
    context.beginPath();
    context.arc(projected.x, projected.y, radius, 0, Math.PI * 2);
    context.fillStyle = selected ? "#f59e0b" : atomTypeToColor(projected.point.atom_type);
    context.globalAlpha = selected ? 1 : 0.62 + projected.depth * 0.32;
    context.fill();
    if (selected) {
      context.globalAlpha = 0.85;
      context.lineWidth = 2;
      context.strokeStyle = "#92400e";
      context.stroke();
    }
  });
  context.globalAlpha = 1;
}

function pickTrajectoryPoint(
  cloud: TrajectoryCloud,
  view: TrajectoryView,
  size: CanvasSize,
  x: number,
  y: number
) {
  const projectedPoints = projectTrajectoryPoints(cloud, view, size);
  let bestPoint: MdDemoTrajectoryPoint | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  let bestZ = Number.NEGATIVE_INFINITY;
  const maxDistance = 20;

  projectedPoints.forEach((projected) => {
    const deltaX = projected.x - x;
    const deltaY = projected.y - y;
    const distance = Math.sqrt(deltaX * deltaX + deltaY * deltaY);
    const isBetterDistance = distance < bestDistance - 0.3;
    const isSameSpotButCloser = Math.abs(distance - bestDistance) <= 0.3 && projected.z > bestZ;
    if (distance <= maxDistance && (isBetterDistance || isSameSpotButCloser)) {
      bestPoint = projected.point;
      bestDistance = distance;
      bestZ = projected.z;
    }
  });

  return bestPoint;
}

function projectTrajectoryPoints(
  cloud: TrajectoryCloud,
  view: TrajectoryView,
  size: CanvasSize
): ProjectedTrajectoryPoint[] {
  const scale = (Math.min(size.width, size.height) * 0.42 * view.zoom) / cloud.radius;
  const cosX = Math.cos(view.rotationX);
  const sinX = Math.sin(view.rotationX);
  const cosY = Math.cos(view.rotationY);
  const sinY = Math.sin(view.rotationY);

  return cloud.points.map((point) => {
    const x = point.x - cloud.center.x;
    const y = point.y - cloud.center.y;
    const z = point.z - cloud.center.z;
    const rotatedX = x * cosY + z * sinY;
    const zAfterY = -x * sinY + z * cosY;
    const rotatedY = y * cosX - zAfterY * sinX;
    const rotatedZ = y * sinX + zAfterY * cosX;
    return {
      point,
      x: size.width / 2 + rotatedX * scale,
      y: size.height / 2 - rotatedY * scale,
      z: rotatedZ,
      depth: clamp((rotatedZ / cloud.radius + 1) / 2, 0, 1),
    };
  });
}

function atomTypeToColor(atomType: string) {
  const colorMap: Record<string, string> = {
    "1": "#64748b",
    "2": "#22c55e",
    "3": "#ef4444",
    "4": "#2563eb",
    "5": "#a16207",
    "6": "#0891b2",
    "7": "#16a34a",
    "8": "#7c3aed",
  };
  return colorMap[atomType] ?? "#475569";
}
