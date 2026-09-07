import {
  Atom,
  CheckCircle2,
  Gauge,
  Info,
  Loader2,
  TableProperties,
  TriangleAlert
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useState,
  type KeyboardEvent,
  type ReactNode
} from "react";
import {
  isMonomerDftArtifactAvailable,
  lowestMonomerDftFrequency,
  resolveMonomerDftRdkitPreparation,
  userFacingMonomerDftMessage
} from "../lib/monomerDftPresentation";
import { fetchMonomerDftArtifactJson } from "../services/api";
import type {
  MonomerDftAtom,
  MonomerDftJobResponse,
  MonomerDftOptimizationStep,
  MonomerDftResult,
  MonomerDftTrajectoryArtifact,
  MonomerDftVector3
} from "../types";
import {
  MoleculeCoordinates3D,
  type MoleculeCoordinateFrame
} from "./MoleculeCoordinates3D";

type ResultTab = "overview" | "spectra" | "atoms";

const RESULT_TABS: Array<{
  id: ResultTab;
  label: string;
  description: string;
  icon: typeof Gauge;
}> = [
  { id: "overview", label: "概览", description: "状态 · 3D", icon: CheckCircle2 },
  { id: "spectra", label: "曲线与频率", description: "优化 · 振动", icon: Gauge },
  { id: "atoms", label: "原子数据", description: "坐标 · 电荷 · 力", icon: Atom }
];

function formatNumber(value: number | null | undefined, digits = 5): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

function geometryStatusLabel(value: string): string {
  if (value === "converged") return "已收敛";
  if (value === "max_steps_reached") return "达到最大步数";
  if (value === "not_optimized") return "未执行优化";
  return "未标注";
}

function Panel({ title, icon, children, className = "" }: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`np-dft-result-panel ${className}`.trim()}>
      <header><span>{icon}</span><h3>{title}</h3></header>
      <div className="np-dft-result-panel__body">{children}</div>
    </section>
  );
}

type ChartPoint = { x: number; y: number };

function LineChart({ points, color, yLabel }: {
  points: ChartPoint[];
  color: string;
  yLabel: string;
}) {
  const width = 620;
  const height = 240;
  const pad = 34;
  if (points.length === 0) {
    return <div className="np-dft-chart-empty">暂无曲线数据</div>;
  }
  const minX = Math.min(...points.map((point) => point.x));
  const maxX = Math.max(...points.map((point) => point.x));
  const minY = Math.min(...points.map((point) => point.y));
  const maxY = Math.max(...points.map((point) => point.y));
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || Math.max(Math.abs(maxY), 1);
  const project = (point: ChartPoint) => ({
    x: pad + ((point.x - minX) / xSpan) * (width - pad * 2),
    y: height - pad - ((point.y - minY) / ySpan) * (height - pad * 2)
  });
  const path = points.map((point, index) => {
    const projected = project(point);
    return `${index === 0 ? "M" : "L"}${projected.x.toFixed(2)},${projected.y.toFixed(2)}`;
  }).join(" ");
  const last = points.at(-1);

  return (
    <div className="np-dft-chart">
      <div className="np-dft-chart-summary">
        <span>最低 <strong>{formatNumber(minY, 5)}</strong></span>
        <span>最高 <strong>{formatNumber(maxY, 5)}</strong></span>
        <span>最终 <strong>{formatNumber(last?.y, 5)}</strong></span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={yLabel}>
        <line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} className="np-dft-chart-axis" />
        <line x1={pad} y1={pad} x2={pad} y2={height - pad} className="np-dft-chart-axis" />
        <path d={path} fill="none" stroke={color} strokeWidth="3" strokeLinejoin="round" />
        {points.length <= 80 ? points.map((point) => {
          const projected = project(point);
          return <circle key={`${point.x}-${point.y}`} cx={projected.x} cy={projected.y} r="2.7" fill={color} />;
        }) : null}
        <text x={pad} y={20}>{formatNumber(maxY, 4)}</text>
        <text x={pad} y={height - 8}>step {formatNumber(minX, 0)}–{formatNumber(maxX, 0)}</text>
      </svg>
      <p>{yLabel}</p>
    </div>
  );
}

export function isImaginaryMonomerDftFrequency(value: number, thresholdCm1: number): boolean {
  return value < thresholdCm1;
}

function FrequencyChart({ values, imaginaryThresholdCm1 }: {
  values: number[];
  imaginaryThresholdCm1: number;
}) {
  if (values.length === 0) return <div className="np-dft-chart-empty">未返回频率</div>;
  const width = 720;
  const height = 250;
  const pad = 36;
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const zeroY = pad + (max / span) * (height - pad * 2);
  const barWidth = Math.max(1, (width - pad * 2) / values.length - 1);
  const imaginaryCount = values.filter((value) => isImaginaryMonomerDftFrequency(value, imaginaryThresholdCm1)).length;

  return (
    <div className="np-dft-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="振动频率谱">
        <line x1={pad} y1={zeroY} x2={width - pad} y2={zeroY} className="np-dft-chart-zero" />
        {values.map((value, index) => {
          const x = pad + index * ((width - pad * 2) / values.length);
          const y = pad + ((max - value) / span) * (height - pad * 2);
          return (
            <line
              key={`${index}-${value}`}
              x1={x}
              x2={x}
              y1={zeroY}
              y2={y}
              stroke={isImaginaryMonomerDftFrequency(value, imaginaryThresholdCm1) ? "#dc2626" : "#0891b2"}
              strokeWidth={barWidth}
            />
          );
        })}
        <text x={pad} y={20}>cm⁻¹</text>
      </svg>
      <p className="np-dft-frequency-legend">
        <span><i className="is-regular" />常规振动模式</span>
        <span><i className="is-imaginary" />虚频模式：低于 {formatNumber(imaginaryThresholdCm1, 1)} cm⁻¹</span>
        <strong>{imaginaryCount} 个虚频模式</strong>
      </p>
    </div>
  );
}

function atomsForCoordinates(
  result: MonomerDftResult,
  coordinates: MonomerDftVector3[],
  includeProperties: boolean,
  frameCharges?: number[] | null
): MonomerDftAtom[] {
  return coordinates.map((position, index) => ({
    index: index + 1,
    atomic_number: result.atoms.atomic_numbers[index] ?? 0,
    element: result.atoms.symbols[index] ?? "X",
    isotope_mass_number: result.schema_version === 2
      ? result.atoms.isotope_mass_numbers[index] ?? 0
      : null,
    atomic_mass_u: result.schema_version === 2
      ? result.atoms.atomic_masses_u[index] ?? null
      : null,
    position_angstrom: position,
    charge_e: includeProperties
      ? result.properties.charges?.values_e[index] ?? frameCharges?.[index] ?? null
      : frameCharges?.[index] ?? null,
    force_ev_per_angstrom: includeProperties
      ? result.properties.forces?.values_eV_per_A[index] ?? null
      : null
  }));
}

const FRAME_COORDINATE_TOLERANCE_ANGSTROM = 1e-8;

function coordinatesMatch(
  left: MonomerDftVector3[],
  right: MonomerDftVector3[],
  tolerance = FRAME_COORDINATE_TOLERANCE_ANGSTROM
): boolean {
  return left.length === right.length && left.every((coordinate, atomIndex) =>
    coordinate.every((value, axisIndex) =>
      Math.abs(value - right[atomIndex][axisIndex]) <= tolerance
    )
  );
}

export function buildFrames(
  result: MonomerDftResult,
  trajectory: MonomerDftTrajectoryArtifact | null
): MoleculeCoordinateFrame[] {
  const trajectoryFrames = trajectory?.frames ?? [];
  const initialTrajectoryFrame = trajectoryFrames.find((point) => coordinatesMatch(
    point.coordinates_angstrom,
    result.geometry.initial_coordinates_angstrom
  ));
  const finalTrajectoryFrame = [...trajectoryFrames].reverse().find((point) => coordinatesMatch(
    point.coordinates_angstrom,
    result.geometry.final_coordinates_angstrom
  ));
  const frames: MoleculeCoordinateFrame[] = [{
    id: "initial",
    label: "初始结构",
    kind: "initial",
    atoms: atomsForCoordinates(
      result,
      result.geometry.initial_coordinates_angstrom,
      false,
      initialTrajectoryFrame?.charges_e
    )
  }];
  for (const [index, point] of trajectoryFrames.entries()) {
    const duplicatesInitial = index === 0 && coordinatesMatch(
      point.coordinates_angstrom,
      result.geometry.initial_coordinates_angstrom
    );
    const duplicatesFinal = index === trajectoryFrames.length - 1 && coordinatesMatch(
      point.coordinates_angstrom,
      result.geometry.final_coordinates_angstrom
    );
    if (duplicatesInitial || duplicatesFinal) continue;
    frames.push({
      id: `step-${point.step}`,
      label: `优化第 ${point.step} 步`,
      kind: "trajectory",
      atoms: atomsForCoordinates(result, point.coordinates_angstrom, false, point.charges_e),
      step: point.step,
      energyEv: point.energy_eV
    });
  }
  frames.push({
    id: "final",
    label: "最终结构",
    kind: "final",
    atoms: atomsForCoordinates(
      result,
      result.geometry.final_coordinates_angstrom,
      true,
      finalTrajectoryFrame?.charges_e
    ),
    energyEv: result.properties.energy.value_eV
  });
  return frames;
}

function Summary({ result }: { result: MonomerDftResult }) {
  const optimization = result.optimization;
  const preparation = resolveMonomerDftRdkitPreparation(result.rdkit);
  const preparationLabel = preparation.state === "converged"
    ? "准备完成"
    : preparation.state === "not_converged"
      ? "已生成，未完全收敛"
      : preparation.state === "not_performed"
        ? "已生成，未进一步优化"
        : "状态未记录";
  const cards = [
    { label: "总能量", value: `${formatNumber(result.properties.energy.value_eV, 7)} eV` },
    { label: "电荷和", value: result.properties.charges ? `${formatNumber(result.properties.charges.sum_e, 6)} e` : "未请求" },
    { label: "最大力", value: result.properties.forces ? `${formatNumber(result.properties.forces.fmax_eV_per_A, 6)} eV/Å` : "未请求" },
    { label: "优化", value: optimization == null
      ? "不适用"
      : optimization.converged
        ? `已收敛 · ${optimization.steps} 步`
        : `未收敛 · ${optimization.steps} / ${optimization.max_steps} 步`, ui: true },
    { label: "初始构型", value: preparationLabel, ui: true }
  ];
  return (
    <div className="np-dft-summary-grid">
      {cards.map(({ label, value, ui }) => (
        <div key={label}><span>{label}</span><strong className={ui ? "is-ui-value" : undefined}>{value}</strong></div>
      ))}
    </div>
  );
}

function AtomTable({ atoms }: { atoms: MonomerDftAtom[] }) {
  return (
    <div className="np-dft-atom-table-wrap" tabIndex={0} aria-label="原子数据表，可横向滚动">
      <table className="np-dft-atom-table">
        <thead>
          <tr>
            <th>#</th><th>元素</th><th>质量 / u</th><th>x / Å</th><th>y / Å</th><th>z / Å</th>
            <th>电荷 / e</th><th>|F| / eV·Å⁻¹</th>
          </tr>
        </thead>
        <tbody>
          {atoms.map((atom) => {
            const force = atom.force_ev_per_angstrom;
            const forceNorm = force ? Math.hypot(force[0], force[1], force[2]) : null;
            const elementLabel = atom.isotope_mass_number != null && atom.isotope_mass_number > 0
              ? `${atom.isotope_mass_number}${atom.element}`
              : atom.element;
            return (
              <tr key={atom.index}>
                <td>{atom.index}</td>
                <td>{elementLabel}</td>
                <td>{formatNumber(atom.atomic_mass_u, 6)}</td>
                {atom.position_angstrom.map((value, index) => <td key={index}>{formatNumber(value, 6)}</td>)}
                <td>{formatNumber(atom.charge_e, 6)}</td>
                <td>{formatNumber(forceNorm, 6)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function minimumAssessmentLabel(value: string): { label: string; tone: string } {
  if (value === "confirmed_minimum") return { label: "确认极小值", tone: "success" };
  if (value === "nonminimum_or_saddle") return { label: "非极小值或鞍点", tone: "error" };
  if (value === "not_converged") return { label: "优化未收敛，无法判定", tone: "warning" };
  return { label: "极小值未评估", tone: "neutral" };
}

function EmptyResult({ job }: { job: MonomerDftJobResponse }) {
  const terminal = job.status === "failed" || job.status === "cancelled";
  return (
    <section className={`np-dft-result-empty${terminal ? " is-terminal" : ""}`} role={terminal ? "alert" : "status"}>
      {terminal ? <TriangleAlert /> : <Loader2 className="np-dft-spin" />}
      <div>
        <strong>{job.status === "failed" ? "计算失败，未生成结果" : job.status === "cancelled" ? "任务已取消，未生成结果" : "正在等待计算结果"}</strong>
        <span>{terminal ? userFacingMonomerDftMessage(
          job.error?.message ?? "返回配置修改输入，或前往任务中心重新计算。",
          {
            code: job.error?.code,
            fallback: "计算未能完成，请返回配置检查输入，或重新运行任务。"
          }
        ) : "完成后显示结构与性质结果。"}</span>
      </div>
    </section>
  );
}

export function MonomerDftResults({ job }: { job: MonomerDftJobResponse }) {
  const result = job.result;
  const [activeTab, setActiveTab] = useState<ResultTab>("overview");
  const [trajectory, setTrajectory] = useState<MonomerDftTrajectoryArtifact | null>(null);
  const [isTrajectoryLoading, setIsTrajectoryLoading] = useState(false);
  const [trajectoryError, setTrajectoryError] = useState<string | null>(null);
  const [trajectoryRevision, setTrajectoryRevision] = useState(0);
  const trajectoryArtifactId = result?.optimization?.trajectory_artifact_id ?? null;

  useEffect(() => setActiveTab("overview"), [job.job_id]);

  useEffect(() => {
    setTrajectory(null);
    setTrajectoryError(null);
    setIsTrajectoryLoading(false);
    if (
      job.status !== "completed" ||
      !trajectoryArtifactId ||
      !job.artifacts.some((artifact) =>
        artifact.artifact_id === trajectoryArtifactId && isMonomerDftArtifactAvailable(job, artifact)
      )
    ) return;
    const controller = new AbortController();
    setIsTrajectoryLoading(true);
    fetchMonomerDftArtifactJson<MonomerDftTrajectoryArtifact>(
      job.job_id,
      trajectoryArtifactId,
      controller.signal
    )
      .then((payload) => setTrajectory(payload))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setTrajectoryError(error instanceof Error
            ? userFacingMonomerDftMessage(error.message, {
              fallback: "暂时无法读取完整优化轨迹，请稍后重试。"
            })
            : "优化轨迹载入失败。");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsTrajectoryLoading(false);
      });
    return () => controller.abort();
  }, [
    job.artifacts,
    job.artifacts_deleted,
    job.artifacts_state,
    job.job_id,
    job.status,
    trajectoryArtifactId,
    trajectoryRevision
  ]);

  const frames = useMemo(() => result ? buildFrames(result, trajectory) : [], [result, trajectory]);
  if (!result) return <EmptyResult job={job} />;

  const trace = result.optimization?.trace ?? [];
  const atoms = atomsForCoordinates(result, result.geometry.final_coordinates_angstrom, true);
  const frequencies = result.properties.frequencies;
  const lowestFrequency = frequencies ? lowestMonomerDftFrequency(frequencies.values_cm_1) : null;
  const hessian = result.properties.hessian;
  const assessment = minimumAssessmentLabel(result.scientific_status.minimum_assessment);
  const rdkitPreparation = resolveMonomerDftRdkitPreparation(result.rdkit);
  const hasExplicitIsotopes = result.schema_version === 2 &&
    result.atoms.isotope_mass_numbers.some((massNumber) => massNumber > 0);
  const warnings = [...new Map(
    [...job.warnings.map((message) => ({ code: "job", message })), ...result.warnings]
      .map((warning) => [warning.message, warning] as const)
  ).values()];

  function handleTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = RESULT_TABS.findIndex((tab) => tab.id === activeTab);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? RESULT_TABS.length - 1
        : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + RESULT_TABS.length) % RESULT_TABS.length;
    const nextTab = RESULT_TABS[nextIndex];
    setActiveTab(nextTab.id);
    document.getElementById(`monomer-dft-result-tab-${nextTab.id}`)?.focus();
  }

  return (
    <div className="np-dft-results">
      <div className="np-dft-result-tabs" role="tablist" aria-label="DFT 结果分析" onKeyDown={handleTabKeyDown}>
        {RESULT_TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              id={`monomer-dft-result-tab-${tab.id}`}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`monomer-dft-result-panel-${tab.id}`}
              tabIndex={activeTab === tab.id ? 0 : -1}
              className={activeTab === tab.id ? "is-active" : ""}
              onClick={() => setActiveTab(tab.id)}
            >
              <Icon /><span><strong>{tab.label}</strong><small>{tab.description}</small></span>
            </button>
          );
        })}
      </div>

      <section
        id={`monomer-dft-result-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={`monomer-dft-result-tab-${activeTab}`}
        className="np-dft-result-tabpanel"
      >
        {activeTab === "overview" ? (
          <div className="np-dft-result-stack">
            {warnings.length > 0 ? (
              <div className="np-dft-result-notice is-warning" role="alert">
                <TriangleAlert />
                <div>{warnings.map((warning, index) => (
                  <p key={`${warning.code}-${index}`}>{userFacingMonomerDftMessage(warning.message, {
                    code: warning.code,
                    fallback: "计算已完成，但存在需要注意的情况，请谨慎使用结果。"
                  })}</p>
                ))}</div>
              </div>
            ) : null}
            {rdkitPreparation.state === "not_performed" ? (
              <div className="np-dft-result-notice is-warning">
                <TriangleAlert />
                <div><strong>初始构型未经进一步优化</strong><p>本次计算直接使用自动生成的三维构型。</p></div>
              </div>
            ) : null}
            {rdkitPreparation.state === "unknown" ? (
              <div className="np-dft-result-notice">
                <Info />
                <div><strong>初始构型准备状态未知</strong><p>该历史结果未记录完整的准备信息。</p></div>
              </div>
            ) : null}

            <Panel title="提交参数与科学状态" icon={<Info />}>
              <dl className="np-dft-request-snapshot">
                <div><dt>标准化结构（SMILES）</dt><dd tabIndex={0} title={result.input.canonical_smiles}>{result.input.canonical_smiles}</dd></div>
                <div><dt>计算类型</dt><dd className="is-ui-value">{result.calculation_type === "single_point" ? "单点计算" : "几何优化"}</dd></div>
                <div><dt>净电荷</dt><dd>{result.input.net_charge}</dd></div>
                <div><dt>多重度</dt><dd>{result.input.multiplicity}</dd></div>
                <div><dt>电子数</dt><dd>{result.input.electron_count}</dd></div>
                <div><dt>几何状态</dt><dd className="is-ui-value">{geometryStatusLabel(result.scientific_status.geometry_status)}</dd></div>
                <div><dt>驻点</dt><dd className="is-ui-value">{result.scientific_status.is_stationary ? "是" : "否"}</dd></div>
              </dl>
            </Panel>
            <Panel title="计算摘要" icon={<CheckCircle2 />}><Summary result={result} /></Panel>
            <MoleculeCoordinates3D frames={frames} calculationType={result.calculation_type} />
            {isTrajectoryLoading ? (
              <div className="np-dft-result-notice" role="status">
                <Loader2 className="np-dft-spin" />正在载入完整优化轨迹…
              </div>
            ) : null}
            {trajectoryError ? (
              <div className="np-dft-result-notice is-warning" role="status">
                <TriangleAlert />
                <div>
                  <strong>轨迹载入失败，已显示初始与最终结构</strong>
                  <p>{trajectoryError}</p>
                  <button type="button" onClick={() => setTrajectoryRevision((value) => value + 1)}>重新载入轨迹</button>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {activeTab === "spectra" ? (
          <div className="np-dft-result-stack">
            {hasExplicitIsotopes && frequencies ? (
              <div className="np-dft-result-notice">
                <Info />
                <div><strong>频率分析已使用指定的同位素质量</strong><p>同位素会影响振动频率，不改变势能面与电子能量。</p></div>
              </div>
            ) : null}
            {trace.length > 0 ? (
              <div className="np-dft-chart-grid">
                <Panel title="优化能量" icon={<Gauge />}>
                  <LineChart
                    points={trace.map((point: MonomerDftOptimizationStep) => ({ x: point.step, y: point.energy_eV }))}
                    color="#2563eb"
                    yLabel="能量 / eV"
                  />
                </Panel>
                <Panel title="优化最大力" icon={<Gauge />}>
                  <LineChart
                    points={trace.map((point) => ({ x: point.step, y: point.fmax_eV_per_A }))}
                    color="#d97706"
                    yLabel="最大原子受力 / eV Å⁻¹"
                  />
                </Panel>
              </div>
            ) : null}
            {frequencies ? (
              <Panel title="振动频率" icon={<Gauge />}>
                <div className="np-dft-spectrum-facts">
                  <span>虚频 <strong>{frequencies.imaginary_mode_count}</strong></span>
                  <span>最低 <strong>{lowestFrequency == null ? "--" : formatNumber(lowestFrequency, 2)} cm⁻¹</strong></span>
                  <span className={`is-${assessment.tone}`}>{assessment.label}</span>
                </div>
                <p className="np-dft-result-help">极小值结论来自本次计算；单点任务不进行该项评估。</p>
                <FrequencyChart
                  values={frequencies.values_cm_1}
                  imaginaryThresholdCm1={frequencies.imaginary_threshold_cm_1}
                />
              </Panel>
            ) : null}
            {hessian ? (
              <Panel title="二阶力常数摘要" icon={<TableProperties />}>
                <div className="np-dft-hessian-grid">
                  <div><span>矩阵形状</span><strong>{hessian.shape[0]} × {hessian.shape[1]}</strong></div>
                  <div><span>最大对称误差</span><strong>{formatNumber(hessian.symmetry_max_abs_eV_per_A2, 8)} eV/Å²</strong></div>
                  <div><span>矩阵对称性</span><strong className="is-ui-value">{hessian.symmetric_within_tolerance ? "符合要求" : "不符合要求"}</strong></div>
                </div>
              </Panel>
            ) : null}
            {trace.length === 0 && !frequencies && !hessian ? (
              <div className="np-dft-empty-state is-compact"><Gauge /><div><strong>没有曲线或频率数据</strong><span>仅在请求对应性质或执行几何优化时显示。</span></div></div>
            ) : null}
          </div>
        ) : null}

        {activeTab === "atoms" ? (
          atoms.length > 0 ? (
            <Panel title="原子坐标、电荷与力" icon={<Atom />}>
              <p className="np-dft-result-help">坐标来自计算完成后的最终构型；未计算的项目显示为“--”。</p>
              <AtomTable atoms={atoms} />
            </Panel>
          ) : (
            <div className="np-dft-empty-state is-compact"><Atom /><div><strong>没有原子数据</strong><span>当前结果未返回可展示的最终坐标。</span></div></div>
          )
        ) : null}

      </section>
    </div>
  );
}
