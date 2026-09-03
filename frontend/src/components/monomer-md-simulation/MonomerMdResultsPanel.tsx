import {
  Activity,
  Atom,
  Ban,
  BarChart3,
  Box,
  Gauge,
  Info,
  LoaderCircle,
  Pause,
  Play,
  Repeat2,
  Rotate3D,
  SkipBack,
  SkipForward,
  Trash2,
  TriangleAlert,
  X
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useState
} from "react";
import type {
  MdDemoTrajectoryPoint,
  MonomerMdJobResponse,
  MonomerMdProtocol,
  MonomerMdSeries,
  MonomerMdSimulationResult,
  MonomerMdTrajectoryPoint,
  MonomerMdTrajectoryPreview,
  MonomerMdTrajectoryTimeline,
  MonomerMdVisualizationStage
} from "../../types";
import { MdTrajectoryPointCloudCanvas } from "../md-simulation/MdTrajectoryExplorer";
import { fetchMonomerMdTrajectoryTimeline } from "../../services/api";
import { monomerMdDemoNotice } from "../../utils/monomerMdPresentation";
import { isRecord } from "./config";
import {
  decodeMonomerMdTrajectoryTimeline,
  monomerMdTimelineFrameDelayMs,
  monomerMdTimelineFramePoints,
  monomerMdTimelineProjectionBounds,
  type DecodedMonomerMdTrajectoryTimeline
} from "./trajectoryTimeline";
import {
  adaptMonomerMdMetrics,
  adaptMonomerMdVisualization,
  clampMonomerMdProgress,
  formatDateTime,
  formatMetricValue,
  formatNumber,
  JOB_STATUS_LABELS,
  monomerMdTrajectoryPreviewPoints,
  monomerMdVisualizationWarningText,
  normalizeMonomerMdSeries,
  PROTOCOL_LABELS,
  translateMonomerMdMessage,
  type SeriesPoint
} from "./presentation";

type ResultTab = "overview" | "curves" | "conformation";

const RESULT_TABS: Array<{
  id: ResultTab;
  label: string;
  description: string;
  icon: typeof Gauge;
}> = [
  { id: "overview", label: "概览", description: "任务快照与科学指标", icon: Gauge },
  { id: "curves", label: "曲线", description: "密度、温度与能量", icon: BarChart3 },
  { id: "conformation", label: "构象", description: "关键帧三维轨迹", icon: Atom }
];

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

type MonomerMdResultsPanelProps = {
  job: MonomerMdJobResponse | null;
  result: MonomerMdSimulationResult | null;
  isLoading: boolean;
  error: string | null;
  cancelling: boolean;
  deleting: boolean;
  onCancel: (job: MonomerMdJobResponse) => void;
  onDelete: (job: MonomerMdJobResponse) => void;
  onClear: () => void;
};

export function MonomerMdResultsPanel({
  job,
  result,
  isLoading,
  error,
  cancelling,
  deleting,
  onCancel,
  onDelete,
  onClear
}: MonomerMdResultsPanelProps) {
  const [activeTab, setActiveTab] = useState<ResultTab>("overview");
  const visualization = useMemo(() => adaptMonomerMdVisualization(result), [result]);
  const [selectedStageId, setSelectedStageId] = useState(visualization.defaultStageId);
  const [trajectoryTimelines, setTrajectoryTimelines] = useState<Record<string, MonomerMdTrajectoryTimeline>>({});

  useEffect(() => {
    setSelectedStageId(visualization.defaultStageId);
    setTrajectoryTimelines({});
  }, [job?.job_id, visualization.defaultStageId]);

  const cacheTrajectoryTimeline = useCallback((cacheKey: string, timeline: MonomerMdTrajectoryTimeline) => {
    setTrajectoryTimelines((current) => current[cacheKey] === timeline
      ? current
      : { ...current, [cacheKey]: timeline });
  }, []);

  const selectedStage = visualization.stages.find((stage) => stage.stage_id === selectedStageId)
    ?? visualization.stages[0];
  const selectedTimelineCacheKey = `${job?.job_id ?? "none"}:${selectedStage.stage_id}`;
  const selectedTimeline = selectedStage.trajectory_timeline
    ?? trajectoryTimelines[selectedTimelineCacheKey]
    ?? null;
  const stageWarningCodes = new Set(
    visualization.stages.flatMap((stage) => stage.warnings ?? [])
  );
  const globalWarnings = visualization.warnings.filter((warning) => {
    const code = warning.includes(":") ? warning.slice(warning.lastIndexOf(":") + 1) : warning;
    return !stageWarningCodes.has(code);
  });

  if (!job) {
    const emptyTitle = isLoading ? "正在恢复任务" : error ? "未能恢复任务" : "尚未选择任务";
    const emptyDescription = isLoading
      ? "正在读取任务的最新状态和已有结果，请稍候。"
      : error
        ? "请从任务中心重新选择一条记录，或提交新的模拟任务。"
        : "请先提交模拟任务，或从任务中心选择已有记录。";
    return (
      <div className="np-mmd-result-empty">
        {isLoading ? <LoaderCircle className="np-mmd-spin" /> : <Activity />}
        <h3>{emptyTitle}</h3>
        <p>{emptyDescription}</p>
        {error ? <div className="np-mmd-inline-error" role="alert">{translateMonomerMdMessage(error)}</div> : null}
      </div>
    );
  }

  const progress = clampMonomerMdProgress(job.progress_percent ?? job.progress);
  const terminal = TERMINAL.has(job.status);
  const jobTimeRange = `${formatDateTime(job.started_at)} / ${formatDateTime(job.finished_at)}`;

  return (
    <div className="np-mmd-results">
      <header className="np-mmd-job-header">
        <div className="np-mmd-job-header__identity">
          <span className="np-mmd-eyebrow">CURRENT JOB</span>
          <div><code title={job.job_id}>{job.job_id}</code><span className={`np-mmd-status-pill is-${job.status}`}>{JOB_STATUS_LABELS[job.status]}</span></div>
          <p>结果对应当前选中任务的提交参数；修改配置不会改变这份结果。</p>
        </div>
        <div className="np-mmd-job-header__actions">
          {!terminal && job.status !== "cancel_requested" ? (
            <button
              type="button"
              className="is-danger-soft"
              disabled={cancelling}
              onClick={() => {
                if (window.confirm("确定取消这个全局任务吗？正在运行的任务可能需要等待 Worker 响应。")) onCancel(job);
              }}
            >{cancelling ? <LoaderCircle className="np-mmd-spin" /> : <Ban />}取消任务</button>
          ) : null}
          {terminal ? (
            <button
              type="button"
              className="is-danger-soft"
              disabled={deleting}
              onClick={() => {
                if (window.confirm("删除后任务记录与深链将无法恢复。确定删除吗？")) onDelete(job);
              }}
            >{deleting ? <LoaderCircle className="np-mmd-spin" /> : <Trash2 />}删除记录</button>
          ) : null}
          <button type="button" onClick={onClear}><X />清除选择</button>
        </div>
        <dl className="np-mmd-job-facts">
          <div><dt>模式 / 协议</dt><dd className="np-mmd-ui-value">{job.run_mode === "demo" ? "真实快速演示" : "正式任务"} · {PROTOCOL_LABELS[job.protocol ?? "DensityDemo"]}</dd></div>
          <div><dt>真实阶段</dt><dd className="np-mmd-ui-value">{job.progress_stage || job.progress_message || "--"}</dd></div>
          <div><dt>排队位置</dt><dd className="np-mmd-ui-value">{job.queue_position == null ? "--" : `第 ${job.queue_position} 位`}</dd></div>
          <div className="np-mmd-job-fact-with-tooltip" data-tooltip={jobTimeRange}>
            <dt>开始 / 结束</dt>
            <dd
              className="np-mmd-job-time-range"
              tabIndex={0}
              title={jobTimeRange}
              aria-label={`开始 / 结束：${jobTimeRange}`}
            >{jobTimeRange}</dd>
          </div>
        </dl>
        <div className="np-mmd-job-progress" aria-label={`任务进度 ${Math.round(progress)}%`}>
          <div><span>{JOB_STATUS_LABELS[job.status]}</span><strong>{Math.round(progress)}%</strong></div>
          <span><i className={job.status === "failed" || job.status === "cancelled" ? "is-error" : ""} style={{ width: `${progress}%` }} /></span>
        </div>
        {error ? <div className="np-mmd-inline-error" role="alert">{translateMonomerMdMessage(error)}</div> : null}
      </header>

      <div className="np-mmd-result-tabs" role="tablist" aria-label="结果分析视图">
        {RESULT_TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              id={`monomer-md-result-tab-${tab.id}`}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`monomer-md-result-panel-${tab.id}`}
              tabIndex={activeTab === tab.id ? 0 : -1}
              className={activeTab === tab.id ? "is-active" : ""}
              onClick={() => setActiveTab(tab.id)}
            >
              <Icon /><span><strong>{tab.label}</strong><small>{tab.description}</small></span>
            </button>
          );
        })}
      </div>

      {visualization.stages.length > 1 ? (
        <StageSelector
          stages={visualization.stages}
          selectedStageId={selectedStage.stage_id}
          onSelect={setSelectedStageId}
        />
      ) : null}

      <StageWarnings warnings={globalWarnings} />

      <section
        id={`monomer-md-result-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={`monomer-md-result-tab-${activeTab}`}
        className="np-mmd-result-panel"
      >
        {activeTab === "overview" ? <Overview job={job} result={result} /> : null}
        {activeTab === "curves" ? <Curves stage={selectedStage} /> : null}
        {activeTab === "conformation" ? (
          <Conformation
            key={selectedTimelineCacheKey}
            jobId={job.job_id}
            stage={selectedStage}
            timeline={selectedTimeline}
            timelineCacheKey={selectedTimelineCacheKey}
            onTimelineLoaded={cacheTrajectoryTimeline}
          />
        ) : null}
      </section>
    </div>
  );
}

function StageSelector({
  stages,
  selectedStageId,
  onSelect
}: {
  stages: MonomerMdVisualizationStage[];
  selectedStageId: string;
  onSelect: (stageId: string) => void;
}) {
  return (
    <div className="np-mmd-stage-selector" role="group" aria-label="选择模拟阶段">
      <span>模拟阶段</span>
      {stages.map((stage) => (
        <button
          key={stage.stage_id}
          type="button"
          className={stage.stage_id === selectedStageId ? "is-active" : ""}
          aria-pressed={stage.stage_id === selectedStageId}
          onClick={() => onSelect(stage.stage_id)}
        >
          <strong>{stage.label}</strong>
          {stage.phase ? <small>{stage.phase === "liquid" ? "液相" : "气相"}</small> : null}
        </button>
      ))}
    </div>
  );
}

function StageWarnings({ warnings }: { warnings: string[] | undefined }) {
  if (!warnings?.length) return null;
  return (
    <div className="np-mmd-stage-warnings" role="status">
      {warnings.map((warning) => (
        <div key={warning}><TriangleAlert /><span>{monomerMdVisualizationWarningText(warning)}</span></div>
      ))}
    </div>
  );
}

function Overview({ job, result }: {
  job: MonomerMdJobResponse;
  result: MonomerMdSimulationResult | null;
}) {
  const protocol = job.protocol ?? result?.protocol ?? "DensityDemo";
  const adapted = result ? adaptMonomerMdMetrics(result, protocol) : null;
  const config = job.config_json;
  const components = isRecord(config?.components)
    ? config.components
    : isRecord(job.components)
      ? job.components
      : {};
  const smiles = isRecord(config?.smiles) ? config.smiles : {};
  const isDemo = job.run_mode === "demo" || protocol === "DensityDemo";
  const formalPhysical = !isDemo && result?.physical_result === true;
  const demoStepCount = job.completed_steps ?? job.requested_steps ?? 300;
  const demoNotice = (result ? monomerMdDemoNotice(result, job) : null) ??
    `这是将由 Worker 实际执行的 ${formatNumber(demoStepCount, 0)} 步 MD 任务；由于步数不足，结果不能作为物理密度估计。`;

  return (
    <div className="np-mmd-overview">
      {isDemo ? (
        <div className="np-mmd-science-warning is-warning"><TriangleAlert /><div><strong>快速演示是真实 MD 计算</strong><span>{demoNotice}</span></div></div>
      ) : formalPhysical ? (
        <div className="np-mmd-science-warning is-success"><Info /><div><strong>后端已标记为正式物理结果</strong><span>仍应结合体系设置、收敛性与误差评估解释数值。</span></div></div>
      ) : (
        <div className="np-mmd-science-warning"><Info /><div><strong>尚无正式物理结果标记</strong><span>只有后端明确返回 physical_result=true 时，本页才标记为正式物理结果。</span></div></div>
      )}

      <div className="np-mmd-overview-grid">
        <SubmissionInformation
          protocol={protocol}
          config={isRecord(config) ? config : {}}
          job={job}
        />
      </div>

      {Object.keys(components).length ? (
        <section className="np-mmd-result-section">
          <div className="np-mmd-section-heading"><div><span className="np-mmd-eyebrow">SYSTEM</span><h3>体系组成</h3></div></div>
          <div className="np-mmd-result-table">
            <div className="np-mmd-result-table__head"><span>组分</span><span>摩尔配比</span><span>SMILES</span></div>
            {Object.entries(components).map(([name, ratio]) => (
              <div key={name}><strong>{name}</strong><span>{formatMetricValue(ratio)}</span><code>{formatMetricValue(smiles[name])}</code></div>
            ))}
          </div>
        </section>
      ) : job.smiles ? (
        <section className="np-mmd-result-section"><div className="np-mmd-section-heading"><div><span className="np-mmd-eyebrow">STRUCTURE</span><h3>提交结构</h3></div></div><code className="np-mmd-smiles-snapshot">{job.smiles}</code></section>
      ) : null}

      {!result ? (
        <div className="np-mmd-empty-state"><LoaderCircle className="np-mmd-spin" />结果尚未生成，当前仅展示真实任务状态。</div>
      ) : (
        <>
          <section className="np-mmd-result-section">
            <div className="np-mmd-section-heading"><div><span className="np-mmd-eyebrow">METRICS</span><h3>摘要指标</h3></div></div>
            {adapted && adapted.metrics.length ? (
              <div className="np-mmd-metric-grid">
                {adapted.metrics.map((metric) => (
                  <article key={metric.key}>
                    <span>{metric.label}</span>
                    <strong>{formatMetricValue(metric.value)}{metric.standardDeviation != null ? ` ± ${formatMetricValue(metric.standardDeviation)}` : ""}</strong>
                    <small>{metric.unit || "无单位元数据"}</small>
                  </article>
                ))}
              </div>
            ) : <div className="np-mmd-empty-state">结果未包含可识别的摘要指标。</div>}
          </section>

          {adapted?.transportRows.length ? (
            <section className="np-mmd-result-section">
              <div className="np-mmd-section-heading"><div><span className="np-mmd-eyebrow">TRANSPORT</span><h3>组分自扩散系数</h3></div></div>
              <div className="np-mmd-transport-table"><div><strong>组分</strong><strong>Dself_inf</strong><strong>单位</strong></div>{adapted.transportRows.map((row) => <div key={row.component}><span>{row.component}</span><code>{formatMetricValue(row.diffusivity)}</code><span>{row.unit || "--"}</span></div>)}</div>
            </section>
          ) : null}
          {adapted?.contractWarnings.map((warning) => <div key={warning} className="np-mmd-inline-error" role="alert">{warning}</div>)}
          {adapted && Object.keys(adapted.raw).length ? (
            <details className="np-mmd-raw-metrics"><summary>原始指标（未识别字段）</summary><pre>{JSON.stringify(adapted.raw, null, 2)}</pre></details>
          ) : null}
        </>
      )}
    </div>
  );
}

type SubmissionFact = {
  label: string;
  value: string;
  useUiFont?: boolean;
};

function SubmissionInformation({
  protocol,
  config,
  job
}: {
  protocol: MonomerMdProtocol;
  config: Record<string, unknown>;
  job: MonomerMdJobResponse;
}) {
  const facts = submissionFacts(protocol, config, job);
  const layoutClass = facts.length >= 8 ? "is-eight" : facts.length >= 6 ? "is-six" : "is-five";
  return (
    <section className="np-mmd-submission-card">
      <div className="np-mmd-section-heading">
        <div><span className="np-mmd-eyebrow">SUBMISSION</span><h3>提交信息</h3></div>
      </div>
      <dl className={`np-mmd-detail-list np-mmd-submission-details ${layoutClass}`}>
        {facts.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd className={fact.useUiFont ? "np-mmd-ui-value" : undefined}>{fact.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function submissionFacts(
  protocol: MonomerMdProtocol,
  config: Record<string, unknown>,
  job: MonomerMdJobResponse
): SubmissionFact[] {
  const temperature = config.temperature;
  const facts: SubmissionFact[] = [
    { label: "协议", value: protocol, useUiFont: true },
    { label: "创建时间", value: formatDateTime(job.created_at) },
    {
      label: "温度",
      value: `${formatMetricValue(temperature)}${temperature != null ? " K" : ""}`
    },
    { label: "目标原子数", value: formatMetricValue(config.natoms) },
    {
      label: "请求 / 完成步数",
      value: `${formatMetricValue(job.requested_steps)} / ${formatMetricValue(job.completed_steps)}`
    }
  ];

  if (protocol === "HVap") {
    facts.push({ label: "体系约束", value: "单组分", useUiFont: true });
  } else if (protocol === "Dielectric") {
    facts.push(
      { label: "NPT 步数", value: protocolInteger(config, "npt_steps", 2_000_000) },
      { label: "NVT 步数", value: protocolInteger(config, "nvt_steps", 6_000_000) },
      { label: "偶极采样间隔", value: protocolInteger(config, "dipole_interval", 500) }
    );
  } else if (protocol === "Compressibility") {
    facts.push({ label: "NPT 步数", value: protocolInteger(config, "npt_steps", 5_000_000) });
  }
  return facts;
}

function protocolInteger(
  config: Record<string, unknown>,
  key: "npt_steps" | "nvt_steps" | "dipole_interval",
  implicitValue: number
) {
  const value = config[key];
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? formatNumber(value, 0)
    : `${formatNumber(implicitValue, 0)} · 隐式缺省`;
}

type CurveKey = "density" | "temperature" | "energy";

type ChartLine = {
  key: string;
  label: string;
  points: SeriesPoint[];
  color: string;
  dashArray?: string;
};

type CurveDefinition = {
  buttonLabel: string;
  title: string;
  unit: string;
  lines: ChartLine[];
};

function seriesUnit(series: MonomerMdSeries | undefined, fallback: string): string {
  return series && !Array.isArray(series) && typeof series.unit === "string"
    ? series.unit
    : fallback;
}

function chartLine(
  key: string,
  label: string,
  series: MonomerMdSeries | undefined,
  valueKeys: string[],
  color: string,
  dashArray?: string
): ChartLine {
  return {
    key,
    label,
    points: normalizeMonomerMdSeries(series, valueKeys),
    color,
    dashArray
  };
}

function Curves({ stage }: { stage: MonomerMdVisualizationStage }) {
  const [selected, setSelected] = useState<CurveKey>("density");
  const definitions: Record<CurveKey, CurveDefinition> = {
    density: {
      buttonLabel: "密度",
      title: "密度",
      unit: seriesUnit(stage.density_series, "g/cm³"),
      lines: [
        chartLine("density", "密度", stage.density_series, ["density", "density_g_cm3", "rho"], "#0284c7")
      ].filter((line) => line.points.length > 0)
    },
    temperature: {
      buttonLabel: "温度",
      title: "温度",
      unit: seriesUnit(stage.temperature_series, "K"),
      lines: [
        chartLine("temperature", "温度", stage.temperature_series, ["temperature", "temperature_k", "temp"], "#0891b2")
      ].filter((line) => line.points.length > 0)
    },
    energy: {
      buttonLabel: "能量",
      title: "总能量",
      unit: seriesUnit(stage.energy_series, "kcal/mol"),
      lines: [
        chartLine("total_energy", "总能量", stage.energy_series, ["energy", "total_energy", "total_energy_kcal_mol"], "#4f46e5")
      ].filter((line) => line.points.length > 0)
    }
  };
  const definition = definitions[selected];
  return (
    <div className="np-mmd-curves">
      <StageWarnings warnings={stage.warnings} />
      <div className="np-mmd-curve-selector" role="group" aria-label="选择结果曲线">
        {(Object.keys(definitions) as CurveKey[]).map((key) => <button key={key} type="button" className={selected === key ? "is-active" : ""} onClick={() => setSelected(key)}>{definitions[key].buttonLabel}</button>)}
      </div>
      <SeriesChart title={definition.title} unit={definition.unit} lines={definition.lines} />
    </div>
  );
}

function SeriesChart({ title, unit, lines }: { title: string; unit: string; lines: ChartLine[] }) {
  if (!lines.length) {
    return <div className="np-mmd-chart-empty"><BarChart3 /><h3>{title}序列为空</h3><p>任务结果没有返回可绘制的数据点。</p></div>;
  }
  const width = 840;
  const height = 320;
  const padX = 54;
  const padY = 30;
  const allPoints = lines.flatMap((line) => line.points);
  const xs = allPoints.map((point) => point.x);
  const ys = allPoints.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || 1;
  const project = (point: SeriesPoint) => {
    const x = padX + ((point.x - minX) / xSpan) * (width - padX * 2);
    const y = height - padY - ((point.y - minY) / ySpan) * (height - padY * 2);
    return { x, y };
  };
  const projectedLines = lines.map((line) => {
    const projected = line.points.map(project);
    return {
      ...line,
      projected,
      polyline: projected.map((point) => `${point.x},${point.y}`).join(" ")
    };
  });
  const onlyLine = lines.length === 1 ? lines[0] : null;
  const current = onlyLine?.points.at(-1)?.y ?? 0;
  const ariaLabel = onlyLine
    ? `${title}，当前 ${formatNumber(current)} ${unit}`
    : `${title}，${lines.map((line) => `${line.label}当前 ${formatNumber(line.points.at(-1)?.y ?? 0)}`).join("，")} ${unit}`;
  const zeroY = minY < 0 && maxY > 0
    ? height - padY - ((0 - minY) / ySpan) * (height - padY * 2)
    : null;
  return (
    <section className="np-mmd-chart" aria-label={`${title}曲线`}>
      <div className="np-mmd-chart__header">
        <div><span className="np-mmd-eyebrow">TIME SERIES</span><h3>{title}</h3><small>横轴为结果返回的时间、步数或帧索引</small></div>
        <div className={`np-mmd-chart-stats${onlyLine ? "" : " is-multiple"}`}>
          {onlyLine ? <>
            <span>当前<strong>{formatNumber(current)}</strong></span>
            <span>最小<strong>{formatNumber(minY)}</strong></span>
            <span>最大<strong>{formatNumber(maxY)}</strong></span>
          </> : lines.map((line) => (
            <span key={line.key}>
              <small className="np-mmd-chart-legend-label"><i style={{ backgroundColor: line.color }} />{line.label}</small>
              <strong>{formatNumber(line.points.at(-1)?.y ?? 0)}</strong>
            </span>
          ))}
          <code>{unit}</code>
        </div>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={ariaLabel}>
        {[0, 1, 2, 3, 4].map((index) => {
          const y = padY + index * ((height - padY * 2) / 4);
          return <line key={index} x1={padX} y1={y} x2={width - padX} y2={y} className="np-mmd-chart-gridline" />;
        })}
        {zeroY == null ? null : <line x1={padX} y1={zeroY} x2={width - padX} y2={zeroY} className="np-mmd-chart-zero-line" />}
        {projectedLines.map((line) => <polyline key={line.key} data-series={line.key} points={line.polyline} fill="none" stroke={line.color} strokeWidth="3" strokeDasharray={line.dashArray} strokeLinecap="round" strokeLinejoin="round" />)}
        {projectedLines.map((line) => {
          const finalPoint = line.projected.at(-1);
          return finalPoint ? <circle key={line.key} cx={finalPoint.x} cy={finalPoint.y} r="4" fill={line.color} /> : null;
        })}
        <text x={padX} y={height - 8}>{formatNumber(minX)}</text>
        <text x={width - padX} y={height - 8} textAnchor="end">{formatNumber(maxX)}</text>
        <text x={8} y={padY + 4}>{formatNumber(maxY)}</text>
        <text x={8} y={height - padY}>{formatNumber(minY)}</text>
        {zeroY == null ? null : <text x={8} y={zeroY + 4}>0</text>}
      </svg>
    </section>
  );
}

function Conformation({
  jobId,
  stage,
  timeline,
  timelineCacheKey,
  onTimelineLoaded
}: {
  jobId: string;
  stage: MonomerMdVisualizationStage;
  timeline: MonomerMdTrajectoryTimeline | null;
  timelineCacheKey: string;
  onTimelineLoaded: (cacheKey: string, timeline: MonomerMdTrajectoryTimeline) => void;
}) {
  const preview = stage.trajectory_preview;
  const points = monomerMdTrajectoryPreviewPoints(preview);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [timelineError, setTimelineError] = useState<string | null>(null);

  useEffect(() => {
    setTimelineError(null);
    if (timeline || !stage.trajectory_timeline_available) {
      setTimelineLoading(false);
      return;
    }
    const controller = new AbortController();
    setTimelineLoading(true);
    void fetchMonomerMdTrajectoryTimeline(jobId, stage.stage_id, controller.signal)
      .then((loadedTimeline) => {
        if (controller.signal.aborted) return;
        onTimelineLoaded(timelineCacheKey, loadedTimeline);
        setTimelineLoading(false);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setTimelineLoading(false);
        setTimelineError(error instanceof Error ? error.message : "轨迹关键帧加载失败");
      });
    return () => controller.abort();
  }, [jobId, onTimelineLoaded, stage.stage_id, stage.trajectory_timeline_available, timeline, timelineCacheKey]);

  if (timeline) {
    return <><StageWarnings warnings={stage.warnings} /><TrajectoryTimelineViewer timeline={timeline} fallbackPoints={points} preview={preview} /></>;
  }
  if (timelineLoading) {
    const frameCount = stage.trajectory_timeline_summary?.sampled_frame_count;
    return <><StageWarnings warnings={stage.warnings} /><div className="np-mmd-chart-empty"><LoaderCircle className="np-mmd-spin" /><h3>正在加载轨迹关键帧</h3><p>正在按需读取{frameCount ? ` ${frameCount} 帧` : ""}有界构象数据。</p></div></>;
  }
  if (timelineError) {
    return <><StageWarnings warnings={stage.warnings} /><div className="np-mmd-conformation-fallback"><div className="np-mmd-inline-error" role="alert">{timelineError}，已回退到最终帧。</div>{points.length ? <ConformationViewer points={points} preview={preview} /> : null}</div></>;
  }
  if (!points.length) {
    return <><StageWarnings warnings={stage.warnings} /><div className="np-mmd-chart-empty"><Atom /><h3>没有构象坐标</h3><p>当前阶段未返回可用的 x / y / z 单帧坐标。</p></div></>;
  }
  return <><StageWarnings warnings={stage.warnings} /><ConformationViewer points={points} preview={preview} /></>;
}

const TIMELINE_SPEEDS = [0.5, 1, 2] as const;

function TrajectoryTimelineViewer({
  timeline,
  fallbackPoints,
  preview
}: {
  timeline: MonomerMdTrajectoryTimeline;
  fallbackPoints: MonomerMdTrajectoryPoint[];
  preview?: MonomerMdTrajectoryPreview | null;
}) {
  const [decoded, setDecoded] = useState<DecodedMonomerMdTrajectoryTimeline | null>(null);
  const [decodeError, setDecodeError] = useState<string | null>(null);
  const [framePosition, setFramePosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);
  const [speed, setSpeed] = useState<(typeof TIMELINE_SPEEDS)[number]>(1);

  useEffect(() => {
    let cancelled = false;
    setDecoded(null);
    setDecodeError(null);
    setFramePosition(0);
    setPlaying(false);
    void decodeMonomerMdTrajectoryTimeline(timeline).then((value) => {
      if (!cancelled) setDecoded(value);
    }).catch((error: unknown) => {
      if (!cancelled) setDecodeError(error instanceof Error ? error.message : "轨迹时间轴无法解码");
    });
    return () => { cancelled = true; };
  }, [timeline]);

  useEffect(() => {
    if (!playing || !decoded || decoded.sampledFrameCount < 2) return;
    if (!loop && framePosition >= decoded.sampledFrameCount - 1) return;
    const delay = monomerMdTimelineFrameDelayMs(decoded, framePosition, speed);
    const timer = window.setTimeout(() => {
      setFramePosition((current) => {
        if (current < decoded.sampledFrameCount - 1) return current + 1;
        if (loop) return 0;
        return current;
      });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [decoded, framePosition, loop, playing, speed]);

  useEffect(() => {
    if (playing && !loop && decoded && framePosition === decoded.sampledFrameCount - 1) {
      setPlaying(false);
    }
  }, [decoded, framePosition, loop, playing]);

  const points = useMemo(
    () => decoded ? monomerMdTimelineFramePoints(decoded, framePosition) : [],
    [decoded, framePosition]
  );
  const canvasPoints = useMemo<MdDemoTrajectoryPoint[]>(() => points.map((point, index) => ({
    atom_id: point.atom_id ?? index + 1,
    chain_id: point.chain_id ?? 0,
    atom_type: point.element ?? point.atom_type ?? "X",
    x: point.x,
    y: point.y,
    z: point.z
  })), [points]);
  const projectionBounds = useMemo(
    () => decoded ? monomerMdTimelineProjectionBounds(decoded) : undefined,
    [decoded]
  );

  if (decodeError) {
    return <div className="np-mmd-conformation-fallback"><div className="np-mmd-inline-error" role="alert">{decodeError}，已回退到最终帧。</div>{fallbackPoints.length ? <ConformationViewer points={fallbackPoints} preview={preview} /> : null}</div>;
  }
  if (!decoded) {
    return <div className="np-mmd-chart-empty"><LoaderCircle className="np-mmd-spin" /><h3>正在加载轨迹关键帧</h3><p>正在解压 60 帧有界构象数据。</p></div>;
  }

  const frame = decoded.frames[framePosition];
  const speedIndex = TIMELINE_SPEEDS.indexOf(speed);
  return (
    <div className="np-mmd-conformation">
      <div className="np-mmd-conformation__header">
        <div><span className="np-mmd-eyebrow">TRAJECTORY TIMELINE</span><h3>构象轨迹</h3><p className="np-mmd-conformation__summary">按模拟时间展示真实 DCD 关键帧，空间尺度固定、帧间不插值；点颜色按元素区分。</p></div>
        <div><span><Rotate3D />拖拽旋转</span><span><Box />滚轮缩放</span><code>{decoded.sampledPoints} / {decoded.totalAtoms} atoms · keyframe {framePosition + 1} / {decoded.sampledFrameCount} · source frame {frame.frame_index} · {frame.time_ps ?? "--"} ps · {decoded.coordinateUnit}</code></div>
      </div>
      <div className="np-mmd-timeline-controls">
        <button type="button" aria-label="第一个关键帧" disabled={framePosition === 0} onClick={() => { setPlaying(false); setFramePosition(0); }}><SkipBack /></button>
        <button type="button" className="is-primary" aria-label={playing ? "暂停构象播放" : "播放构象关键帧"} onClick={() => {
          if (!playing && framePosition === decoded.sampledFrameCount - 1) setFramePosition(0);
          setPlaying((current) => !current);
        }}>{playing ? <Pause /> : <Play />}</button>
        <button type="button" aria-label="下一个关键帧" disabled={framePosition === decoded.sampledFrameCount - 1} onClick={() => { setPlaying(false); setFramePosition((current) => Math.min(decoded.sampledFrameCount - 1, current + 1)); }}><SkipForward /></button>
        <label><span>关键帧 {framePosition + 1} / {decoded.sampledFrameCount}</span><input type="range" min="0" max={decoded.sampledFrameCount - 1} step="1" value={framePosition} aria-label="选择构象关键帧" onChange={(event) => { setPlaying(false); setFramePosition(Number(event.target.value)); }} /></label>
        <button type="button" aria-label="切换播放速度" onClick={() => setSpeed(TIMELINE_SPEEDS[(speedIndex + 1) % TIMELINE_SPEEDS.length])}>{speed}×</button>
        <button type="button" aria-label="循环播放" aria-pressed={loop} className={loop ? "is-active" : ""} onClick={() => setLoop((current) => !current)}><Repeat2 /></button>
        <code>{decoded.sampledFrameCount} 个关键帧 / {decoded.sourceFrameCount} 个源帧</code>
      </div>
      <MdTrajectoryPointCloudCanvas
        points={canvasPoints}
        projectionBounds={projectionBounds}
        showAtomLegend
        helpText="拖拽旋转，滚轮缩放；使用时间轴查看真实关键帧"
        hudLabel={`frame ${frame.frame_index}${frame.time_ps == null ? "" : ` · ${formatNumber(frame.time_ps)} ps`}`}
        ariaLabel={`第 ${framePosition + 1} 个关键帧，源帧 ${frame.frame_index}，采样 ${decoded.sampledPoints} 个原子、总计 ${decoded.totalAtoms} 个原子`}
      />
    </div>
  );
}

function ConformationViewer({ points, preview }: { points: MonomerMdTrajectoryPoint[]; preview?: MonomerMdTrajectoryPreview | null }) {
  const canvasPoints = useMemo<MdDemoTrajectoryPoint[]>(() => points.map((point, index) => ({
    atom_id: point.atom_id ?? index + 1,
    chain_id: point.chain_id ?? 0,
    atom_type: point.element ?? point.atom_type ?? "X",
    x: point.x,
    y: point.y,
    z: point.z
  })), [points]);

  return (
    <div className="np-mmd-conformation">
      <div className="np-mmd-conformation__header">
        <div><span className="np-mmd-eyebrow">SINGLE FRAME</span><h3>构象预览</h3><p>真实 x / y / z 坐标的单帧投影，不代表完整轨迹，也不提供原子距离分析。</p></div>
        <div><span><Rotate3D />拖拽旋转</span><span><Box />滚轮缩放</span><code>{preview?.sampled_points ?? points.length} / {preview?.total_atoms ?? points.length} atoms · frame {preview?.frame_index ?? "--"} · {preview?.time_ps ?? "--"} ps · {preview?.coordinate_unit ?? "coordinate unit unknown"}</code></div>
      </div>
      <MdTrajectoryPointCloudCanvas
        points={canvasPoints}
        showAtomLegend
        helpText="拖拽旋转，滚轮缩放；本视图不提供距离分析"
        hudLabel={`frame ${preview?.frame_index ?? "--"}`}
        ariaLabel={`采样 ${preview?.sampled_points ?? points.length} 个原子、总计 ${preview?.total_atoms ?? points.length} 个原子的可交互单帧构象预览`}
      />
    </div>
  );
}
