import {
  Activity,
  Atom,
  BarChart3,
  CircleAlert,
  Clock3,
  Database,
  Droplets,
  Info,
  Layers3,
  LoaderCircle,
  PlayCircle,
  Route,
  Thermometer,
  TriangleAlert,
  Zap,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import type {
  MdDemoAtomDistanceResponse,
  MdDemoRunRequest,
  MdDemoRunResponse,
} from "../../types";
import { MD_DEMO_PROGRESS_STEPS, progressStepFor } from "./config";
import {
  MdTrajectoryExplorer,
  type MdAtomSelectionSlots,
} from "./MdTrajectoryExplorer";
import { displayMdUnit, MdSeriesChart } from "./MdSeriesChart";

type ResultsTab = "overview" | "curves" | "trajectory";

type MdSimulationResultsPanelProps = {
  workspaceNavigation: ReactNode;
  loading: boolean;
  error: string | null;
  progress: number;
  data: MdDemoRunResponse | null;
  attemptSnapshot: MdDemoRunRequest | null;
  resultSnapshot: MdDemoRunRequest | null;
  stale: boolean;
  distance: MdDemoAtomDistanceResponse | null;
  distanceLoading: boolean;
  distanceError: string | null;
  onShowInput: () => void;
  onSelectionChange: () => void;
  onCalculateDistance: (atomId1: number, atomId2: number) => void;
};

const SERIES_LABELS: Record<string, string> = {
  density: "密度",
  temp: "温度",
  toteng: "总能量",
  kineng: "动能",
  poteng: "势能",
  atom_distance: "原子对距离",
};

function formatNumber(value: number, digits = 3) {
  if (!Number.isFinite(value)) return "--";
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: digits,
  }).format(value);
}

function formatQueryTime(value: number) {
  if (!Number.isFinite(value)) return "--";
  return value >= 1000
    ? `${(value / 1000).toFixed(2)} s`
    : `${value.toFixed(1)} ms`;
}

function ProgressOverview({ progress }: { progress: number }) {
  const current = progressStepFor(progress);
  return (
    <div className="np-md-progress-overview" aria-label="MD 结果准备进度">
      <div className="np-md-progress-heading">
        <div>
          <span>结果准备进度</span>
          <strong>{current.label}</strong>
          <p>{current.detail}</p>
        </div>
        <b>
          {progress}
          <small>/100</small>
        </b>
      </div>
      <div
        className="np-md-progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <i style={{ width: `${progress}%` }} />
      </div>
      <ol className="np-md-progress-steps">
        {MD_DEMO_PROGRESS_STEPS.map((step, index) => {
          const previous =
            index === 0 ? 0 : MD_DEMO_PROGRESS_STEPS[index - 1].threshold;
          const complete = progress > step.threshold || progress === 100;
          const active = progress > previous && progress <= step.threshold;
          return (
            <li
              key={step.label}
              className={complete ? "is-complete" : active ? "is-active" : ""}
            >
              <span>
                {complete ? (
                  index + 1
                ) : active ? (
                  <LoaderCircle className="np-sw-spin" />
                ) : (
                  index + 1
                )}
              </span>
              <div>
                <strong>{step.label}</strong>
                <p>{step.detail}</p>
              </div>
            </li>
          );
        })}
      </ol>
      <div className="np-md-demo-boundary is-compact">
        正在读取并整理示例任务的真实 MD 计算结果。
      </div>
    </div>
  );
}

function ResultOverview({
  data,
  snapshot,
}: {
  data: MdDemoRunResponse;
  snapshot: MdDemoRunRequest;
}) {
  const [activeStage, setActiveStage] = useState(
    data.summary.primary_stage || data.stages[0]?.stage_id || "",
  );
  const selectedStage =
    data.stages.find((stage) => stage.stage_id === activeStage) ??
    data.stages[0];
  useEffect(() => {
    setActiveStage(
      data.summary.primary_stage || data.stages[0]?.stage_id || "",
    );
  }, [data]);

  return (
    <div className="np-md-overview-results">
      <section className="np-md-result-identity">
        <div className="is-run">
          <span className="np-md-identity-icon">
            <Database aria-hidden="true" />
          </span>
          <div className="np-md-identity-copy">
            <span>模拟任务</span>
            <strong>{data.run_id}</strong>
          </div>
          <em>
            <i /> 已完成
          </em>
        </div>
        <div className="is-time">
          <span className="np-md-identity-icon">
            <Clock3 aria-hidden="true" />
          </span>
          <div className="np-md-identity-copy">
            <span>结果加载用时</span>
            <strong>{formatQueryTime(data.query_time_ms)}</strong>
          </div>
        </div>
      </section>

      <section className="np-md-result-kpis" aria-label="MD 模拟结果摘要">
        <div className="is-density">
          <span className="np-md-kpi-icon">
            <Droplets aria-hidden="true" />
          </span>
          <div>
            <span>最终密度</span>
            <strong>{formatNumber(data.summary.final_density_g_cm3, 4)}</strong>
            <small>g/cm³</small>
          </div>
          <i className="np-md-kpi-signal" aria-hidden="true" />
        </div>
        <div className="is-temperature">
          <span className="np-md-kpi-icon">
            <Thermometer aria-hidden="true" />
          </span>
          <div>
            <span>平均温度</span>
            <strong>{formatNumber(data.summary.mean_temperature_k, 2)}</strong>
            <small>K</small>
          </div>
          <i className="np-md-kpi-signal" aria-hidden="true" />
        </div>
        <div className="is-energy">
          <span className="np-md-kpi-icon">
            <Zap aria-hidden="true" />
          </span>
          <div>
            <span>平均总能量</span>
            <strong>
              {formatNumber(data.summary.mean_total_energy_kcal_mol, 2)}
            </strong>
            <small>kcal/mol</small>
          </div>
          <i className="np-md-kpi-signal" aria-hidden="true" />
        </div>
      </section>

      <section className="np-md-result-card">
        <header>
          <div className="np-md-card-heading">
            <span className="np-md-card-mark">
              <Atom aria-hidden="true" />
            </span>
            <div>
              <h3>结果体系规模</h3>
              <p>展示计算结果中记录的体系信息。</p>
            </div>
          </div>
          <small className="np-md-card-code">体系信息</small>
        </header>
        <dl className="np-md-compact-metrics">
          <div>
            <span>01</span>
            <dt>原子数</dt>
            <dd>{formatNumber(data.summary.n_atoms, 0)}</dd>
          </div>
          <div>
            <span>02</span>
            <dt>链数</dt>
            <dd>{formatNumber(data.summary.n_chains, 0)}</dd>
          </div>
          <div>
            <span>03</span>
            <dt>轨迹帧数</dt>
            <dd>{formatNumber(data.summary.n_frames, 0)}</dd>
          </div>
          <div>
            <span>04</span>
            <dt>计算用时</dt>
            <dd>{formatNumber(data.summary.elapsed_seconds, 1)} s</dd>
          </div>
        </dl>
        <div className="np-md-scale-footnote">
          <CircleAlert aria-hidden="true" />
          <div>
            <strong>真实计算结果</strong>
            <span>体系规模和统计数据来自示例结构已完成的 MD 计算。</span>
          </div>
        </div>
      </section>

      <section className="np-md-result-card">
        <header>
          <div className="np-md-card-heading">
            <span className="np-md-card-mark is-cyan">
              <Layers3 aria-hidden="true" />
            </span>
            <div>
              <h3>阶段结果</h3>
              <p>选择阶段查看体系和盒子信息。</p>
            </div>
          </div>
          <small className="np-md-card-code">EQ 阶段</small>
        </header>
        <div
          className="np-md-stage-tabs"
          role="tablist"
          aria-label="MD 阶段结果"
        >
          {data.stages.map((stage, index) => (
            <button
              key={stage.stage_id}
              type="button"
              role="tab"
              aria-selected={stage.stage_id === selectedStage?.stage_id}
              className={
                stage.stage_id === selectedStage?.stage_id ? "is-active" : ""
              }
              onClick={() => setActiveStage(stage.stage_id)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{stage.label}</strong>
            </button>
          ))}
        </div>
        {selectedStage ? (
          <div className="np-md-stage-detail">
            <p>
              <Activity aria-hidden="true" />
              <span>{selectedStage.description}</span>
            </p>
            <dl>
              <div>
                <dt>原子</dt>
                <dd>{formatNumber(selectedStage.n_atoms, 0)}</dd>
              </div>
              <div>
                <dt>链</dt>
                <dd>{formatNumber(selectedStage.n_chains, 0)}</dd>
              </div>
              <div>
                <dt>帧</dt>
                <dd>{formatNumber(selectedStage.n_frames, 0)}</dd>
              </div>
              <div>
                <dt>时间步长</dt>
                <dd>{formatNumber(selectedStage.dt_ps, 3)} ps</dd>
              </div>
              <div>
                <dt>盒子尺寸</dt>
                <dd>
                  {formatNumber(selectedStage.box.lx, 2)} ×{" "}
                  {formatNumber(selectedStage.box.ly, 2)} ×{" "}
                  {formatNumber(selectedStage.box.lz, 2)} Å
                </dd>
              </div>
            </dl>
          </div>
        ) : (
          <div className="np-md-empty-chart">没有可显示的阶段结果。</div>
        )}
      </section>

      <details className="np-md-request-snapshot">
        <summary>查看运行参数</summary>
        <dl>
          <div>
            <dt>SMILES</dt>
            <dd>{snapshot.smiles}</dd>
          </div>
          <div>
            <dt>温度</dt>
            <dd>{snapshot.temperature} K</dd>
          </div>
          <div>
            <dt>压力</dt>
            <dd>{snapshot.pressure} atm</dd>
          </div>
          <div>
            <dt>目标原子数</dt>
            <dd>{snapshot.n_atom}</dd>
          </div>
          <div>
            <dt>链数</dt>
            <dd>{snapshot.n_chain}</dd>
          </div>
          <div>
            <dt>力场</dt>
            <dd>{snapshot.forcefield}</dd>
          </div>
        </dl>
      </details>
    </div>
  );
}

export function MdSimulationResultsPanel({
  workspaceNavigation,
  loading,
  error,
  progress,
  data,
  attemptSnapshot,
  resultSnapshot,
  stale,
  distance,
  distanceLoading,
  distanceError,
  onShowInput,
  onSelectionChange,
  onCalculateDistance,
}: MdSimulationResultsPanelProps) {
  const [activeTab, setActiveTab] = useState<ResultsTab>("overview");
  const [activeSeriesKey, setActiveSeriesKey] = useState("density");
  const [selections, setSelections] = useState<MdAtomSelectionSlots>([
    null,
    null,
  ]);
  const series = useMemo(
    () => (data ? [data.density_series, ...data.thermo_series] : []),
    [data],
  );
  const selectedSeries =
    series.find((item) => item.key === activeSeriesKey) ?? series[0] ?? null;
  const selectedSeriesSummary = useMemo(() => {
    if (!selectedSeries?.points.length) return null;
    const values = selectedSeries.points.map((point) => point.value);
    const times = selectedSeries.points.map((point) => point.time_ps);
    return {
      count: selectedSeries.points.length,
      min: Math.min(...values),
      max: Math.max(...values),
      start: Math.min(...times),
      end: Math.max(...times),
    };
  }, [selectedSeries]);

  useEffect(() => {
    setActiveTab("overview");
    setActiveSeriesKey(data?.density_series.key ?? "density");
    setSelections([null, null]);
  }, [data?.run_id]);

  useEffect(() => {
    if (!attemptSnapshot) return;
    setActiveTab("overview");
    setSelections([null, null]);
  }, [attemptSnapshot]);

  const status = loading
    ? `结果准备 ${progress}%`
    : error
      ? "运行失败"
      : data
        ? "结果已就绪"
        : "等待运行";
  const canExplore = Boolean(data && resultSnapshot);

  function changeSelections(next: MdAtomSelectionSlots) {
    const changed = next.some(
      (selection, index) => selection?.atom_id !== selections[index]?.atom_id,
    );
    setSelections(next);
    if (changed) onSelectionChange();
  }

  return (
    <section
      id="md-simulation-results-panel"
      className="np-md-results-surface"
      role="tabpanel"
      aria-labelledby="md-simulation-results-tab"
    >
      <header className="np-md-view-header np-md-results-header">
        <div className="np-md-view-heading np-md-results-heading">
          <span className="np-md-surface-mark">
            <Activity aria-hidden="true" />
          </span>
          <div>
            <h2>MD 模拟结果</h2>
            <p>{status}</p>
          </div>
        </div>
        <div className="np-md-results-toolbar">
          <span className="np-md-view-badge">
            <Info aria-hidden="true" />
            真实计算结果
          </span>
        </div>
      </header>

      {workspaceNavigation}

      <div className="np-md-results-body">
        <div
          className="np-md-result-tabs"
          role="tablist"
          aria-label="MD 模拟结果分类"
        >
          <button
            type="button"
            role="tab"
            aria-label="概览"
            aria-selected={activeTab === "overview"}
            className={activeTab === "overview" ? "is-active" : ""}
            onClick={() => setActiveTab("overview")}
          >
            <span className="np-md-result-tab-icon">
              <Activity aria-hidden="true" />
            </span>
            <span className="np-md-result-tab-copy">
              <strong>概览</strong>
              <small>关键指标与阶段</small>
            </span>
          </button>
          <button
            type="button"
            role="tab"
            aria-label="曲线"
            disabled={!canExplore}
            aria-selected={activeTab === "curves"}
            className={activeTab === "curves" ? "is-active" : ""}
            onClick={() => setActiveTab("curves")}
          >
            <span className="np-md-result-tab-icon">
              <BarChart3 aria-hidden="true" />
            </span>
            <span className="np-md-result-tab-copy">
              <strong>曲线</strong>
              <small>时序变化分析</small>
            </span>
          </button>
          <button
            type="button"
            role="tab"
            aria-label="轨迹"
            disabled={!canExplore}
            aria-selected={activeTab === "trajectory"}
            className={activeTab === "trajectory" ? "is-active" : ""}
            onClick={() => setActiveTab("trajectory")}
          >
            <span className="np-md-result-tab-icon">
              <Route aria-hidden="true" />
            </span>
            <span className="np-md-result-tab-copy">
              <strong>轨迹</strong>
              <small>原子空间与距离</small>
            </span>
          </button>
        </div>

        {stale && data && !loading ? (
          <div className="np-md-stale-notice" role="status">
            <CircleAlert />
            <span>这些结果基于上一次运行；当前输入已更改。</span>
          </div>
        ) : null}

        {activeTab === "overview" ? (
          loading ? (
            <ProgressOverview progress={progress} />
          ) : error && !data ? (
            <div className="np-sw-result-state is-danger">
              <span>
                <TriangleAlert />
              </span>
              <strong>MD 模拟运行失败</strong>
              <p>{error}</p>
              <button
                type="button"
                className="np-sw-secondary-button"
                onClick={onShowInput}
              >
                返回检查输入
              </button>
            </div>
          ) : data && resultSnapshot ? (
            <>
              {error ? (
                <div className="np-md-run-error">
                  <TriangleAlert />
                  <span>{error} 已保留上一份成功结果。</span>
                </div>
              ) : null}
              <ResultOverview data={data} snapshot={resultSnapshot} />
            </>
          ) : (
            <div className="np-sw-result-state">
              <span>
                <PlayCircle />
              </span>
              <strong>等待 MD 模拟</strong>
              <p>完成输入并开始模拟后，运行过程和计算结果会显示在这里。</p>
            </div>
          )
        ) : null}

        {activeTab === "curves" && data ? (
          <div className="np-md-curves-panel">
            <section className="np-md-series-summary">
              <span className="np-md-series-summary__mark">
                <BarChart3 aria-hidden="true" />
              </span>
              <div>
                <small>当前指标</small>
                <strong>
                  {selectedSeries
                    ? SERIES_LABELS[selectedSeries.key] ?? selectedSeries.label
                    : "等待数据"}
                </strong>
                <p>选择指标查看完整时间序列和数值范围。</p>
              </div>
              <dl>
                <div>
                  <dt>数据点</dt>
                  <dd>{selectedSeriesSummary?.count ?? 0}</dd>
                </div>
                <div>
                  <dt>时间范围</dt>
                  <dd>
                    {selectedSeriesSummary
                      ? `${formatNumber(selectedSeriesSummary.start, 1)}–${formatNumber(selectedSeriesSummary.end, 1)} ps`
                      : "--"}
                  </dd>
                </div>
                <div>
                  <dt>数值范围</dt>
                  <dd>
                    {selectedSeriesSummary && selectedSeries
                      ? `${formatNumber(selectedSeriesSummary.min, 3)}–${formatNumber(selectedSeriesSummary.max, 3)} ${displayMdUnit(selectedSeries.unit)}`
                      : "--"}
                  </dd>
                </div>
              </dl>
            </section>
            <div
              className="np-md-series-selector"
              role="tablist"
              aria-label="MD 结果曲线"
            >
              {series.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  role="tab"
                  aria-selected={selectedSeries?.key === item.key}
                  className={
                    selectedSeries?.key === item.key ? "is-active" : ""
                  }
                  onClick={() => setActiveSeriesKey(item.key)}
                >
                  <span className="np-md-series-signal" aria-hidden="true">
                    <i />
                  </span>
                  <span>
                    <strong>{SERIES_LABELS[item.key] ?? item.label}</strong>
                    <small>{displayMdUnit(item.unit)}</small>
                  </span>
                </button>
              ))}
            </div>
            {selectedSeries ? (
              <section className="np-md-result-card np-md-series-card">
                <header>
                  <div className="np-md-card-heading">
                    <span className="np-md-card-mark is-cyan">
                      <BarChart3 aria-hidden="true" />
                    </span>
                    <div>
                      <h3>
                        {SERIES_LABELS[selectedSeries.key] ??
                          selectedSeries.label}
                        曲线
                      </h3>
                      <p>
                        横轴为模拟时间，纵轴单位为{" "}
                        {displayMdUnit(selectedSeries.unit)}。
                      </p>
                    </div>
                  </div>
                  <small className="np-md-card-code">
                    {selectedSeries.points.length} 个数据点
                  </small>
                </header>
                <MdSeriesChart
                  series={selectedSeries}
                  label={
                    SERIES_LABELS[selectedSeries.key] ?? selectedSeries.label
                  }
                  color={
                    selectedSeries.key === "density" ? "#0891b2" : "#2563eb"
                  }
                />
              </section>
            ) : (
              <div className="np-md-empty-chart">没有可显示的曲线。</div>
            )}
          </div>
        ) : null}

        {activeTab === "trajectory" && data ? (
          <MdTrajectoryExplorer
            points={data.trajectory_preview.points}
            timePs={data.trajectory_preview.time_ps}
            selections={selections}
            distance={distance}
            distanceLoading={distanceLoading}
            distanceError={distanceError}
            onSelectionChange={changeSelections}
            onCalculate={() => {
              if (selections[0] && selections[1])
                onCalculateDistance(
                  selections[0].atom_id,
                  selections[1].atom_id,
                );
            }}
            onClear={() => changeSelections([null, null])}
          />
        ) : null}
      </div>
    </section>
  );
}
