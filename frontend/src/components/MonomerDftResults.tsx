import {
  Atom,
  CheckCircle2,
  Download,
  FileArchive,
  Gauge,
  Info,
  Loader2,
  TableProperties,
  Timer,
  Trash2,
  TriangleAlert
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  fetchMonomerDftArtifactJson,
  getMonomerDftArtifactUrl,
  getMonomerDftBundleUrl
} from "../services/api";
import {
  effectiveMonomerDftArtifactsState,
  formatMonomerDftRdkitPreparation,
  hasAvailableMonomerDftArtifacts,
  isMonomerDftArtifactAvailable,
  lowestMonomerDftFrequency,
  resolveMonomerDftRdkitPreparation,
  selectMonomerDftDisplayTimings
} from "../lib/monomerDftPresentation";
import type {
  MonomerDftAtom,
  MonomerDftJobResponse,
  MonomerDftOptimizationStep,
  MonomerDftResult,
  MonomerDftTrajectoryArtifact,
  MonomerDftVector3
} from "../types";
import { MoleculeCoordinates3D, type MoleculeCoordinateFrame } from "./MoleculeCoordinates3D";
import { Button } from "./ui/button";

function formatNumber(value: number | null | undefined, digits = 5): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(1)} GiB`;
}

function Panel({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"><div className="mb-4 flex items-center gap-2 text-sm font-semibold text-slate-950">{icon}{title}</div>{children}</section>;
}

type ChartPoint = { x: number; y: number };

function LineChart({ points, color, yLabel }: { points: ChartPoint[]; color: string; yLabel: string }) {
  const width = 520;
  const height = 180;
  const pad = 28;
  if (points.length === 0) return <div className="flex h-[180px] items-center justify-center text-xs text-slate-400">暂无曲线数据</div>;
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
  return <div><svg viewBox={`0 0 ${width} ${height}`} className="h-[180px] w-full" role="img" aria-label={yLabel}><line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} stroke="#cbd5e1" /><line x1={pad} y1={pad} x2={pad} y2={height - pad} stroke="#cbd5e1" /><path d={path} fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" />{points.length <= 80 ? points.map((point) => { const projected = project(point); return <circle key={`${point.x}-${point.y}`} cx={projected.x} cy={projected.y} r="2.2" fill={color} />; }) : null}<text x={pad} y={16} fill="#64748b" fontSize="10">{formatNumber(maxY, 4)}</text><text x={pad} y={height - 5} fill="#64748b" fontSize="10">step {formatNumber(minX, 0)}–{formatNumber(maxX, 0)}</text></svg><div className="text-center text-[11px] text-slate-500">{yLabel}</div></div>;
}

export function isImaginaryMonomerDftFrequency(value: number, thresholdCm1: number): boolean {
  return value < thresholdCm1;
}

function FrequencyChart({ values, imaginaryThresholdCm1 }: { values: number[]; imaginaryThresholdCm1: number }) {
  if (values.length === 0) return <div className="flex h-[180px] items-center justify-center text-xs text-slate-400">未返回频率</div>;
  const width = 620;
  const height = 190;
  const pad = 28;
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const zeroY = pad + (max / span) * (height - pad * 2);
  const barWidth = Math.max(1, (width - pad * 2) / values.length - 1);
  return <svg viewBox={`0 0 ${width} ${height}`} className="h-[190px] w-full" role="img" aria-label="振动频率谱"><line x1={pad} y1={zeroY} x2={width - pad} y2={zeroY} stroke="#64748b" strokeDasharray="3 3" />{values.map((value, index) => { const x = pad + index * ((width - pad * 2) / values.length); const y = pad + ((max - value) / span) * (height - pad * 2); return <line key={`${index}-${value}`} x1={x} x2={x} y1={zeroY} y2={y} stroke={isImaginaryMonomerDftFrequency(value, imaginaryThresholdCm1) ? "#ef4444" : "#0ea5e9"} strokeWidth={barWidth} />; })}<text x={pad} y={15} fill="#64748b" fontSize="10">cm⁻¹</text><text x={width - 150} y={height - 6} fill="#ef4444" fontSize="10">红色：低于 {formatNumber(imaginaryThresholdCm1, 1)} cm⁻¹</text></svg>;
}

function atomsForCoordinates(result: MonomerDftResult, coordinates: MonomerDftVector3[], includeProperties: boolean): MonomerDftAtom[] {
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
    charge_e: includeProperties ? result.properties.charges?.values_e[index] ?? null : null,
    force_ev_per_angstrom: includeProperties ? result.properties.forces?.values_eV_per_A[index] ?? null : null
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

export function buildFrames(result: MonomerDftResult, trajectory: MonomerDftTrajectoryArtifact | null): MoleculeCoordinateFrame[] {
  const frames: MoleculeCoordinateFrame[] = [{
    id: "initial",
    label: "初始结构",
    kind: "initial",
    atoms: atomsForCoordinates(result, result.geometry.initial_coordinates_angstrom, false)
  }];
  const trajectoryFrames = trajectory?.frames ?? [];
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
      atoms: atomsForCoordinates(result, point.coordinates_angstrom, false),
      step: point.step,
      energyEv: point.energy_eV
    });
  }
  frames.push({
    id: "final",
    label: "最终结构",
    kind: "final",
    atoms: atomsForCoordinates(result, result.geometry.final_coordinates_angstrom, true),
    energyEv: result.properties.energy.value_eV
  });
  return frames;
}

function Summary({ result }: { result: MonomerDftResult }) {
  const optimization = result.optimization;
  const cards = [
    ["总能量", `${formatNumber(result.properties.energy.value_eV, 7)} eV`],
    ["电荷和", result.properties.charges ? `${formatNumber(result.properties.charges.sum_e, 6)} e` : "未请求"],
    ["最大力", result.properties.forces ? `${formatNumber(result.properties.forces.fmax_eV_per_A, 6)} eV/Å` : "未请求"],
    ["优化", optimization == null ? "不适用" : optimization.converged ? `已收敛 · ${optimization.steps} 步` : `未收敛 · ${optimization.steps} / ${optimization.max_steps} 步`],
    ["RDKit 构象", formatMonomerDftRdkitPreparation(result.rdkit)]
  ];
  return <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{cards.map(([label, value]) => <div key={label} className="rounded-xl border border-slate-100 bg-slate-50 p-3"><div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">{label}</div><div className="mt-1 break-words text-sm font-semibold text-slate-950">{value}</div></div>)}</div>;
}

function AtomTable({ atoms }: { atoms: MonomerDftAtom[] }) {
  return <div className="max-h-[360px] overflow-auto"><table className="w-full min-w-[760px] text-left text-xs"><thead className="sticky top-0 bg-white text-slate-500"><tr><th className="px-2 py-2">#</th><th className="px-2 py-2">元素</th><th className="px-2 py-2">质量 / u</th><th className="px-2 py-2">x / Å</th><th className="px-2 py-2">y / Å</th><th className="px-2 py-2">z / Å</th><th className="px-2 py-2">电荷 / e</th><th className="px-2 py-2">|F| / eV Å⁻¹</th></tr></thead><tbody>{atoms.map((atom) => { const force = atom.force_ev_per_angstrom; const forceNorm = force ? Math.hypot(force[0], force[1], force[2]) : null; const elementLabel = atom.isotope_mass_number != null && atom.isotope_mass_number > 0 ? `${atom.isotope_mass_number}${atom.element}` : atom.element; return <tr key={atom.index} className="border-t border-slate-100 font-mono text-slate-700"><td className="px-2 py-2">{atom.index}</td><td className="px-2 py-2 font-sans font-semibold">{elementLabel}</td><td className="px-2 py-2">{formatNumber(atom.atomic_mass_u, 6)}</td>{atom.position_angstrom.map((value, index) => <td key={index} className="px-2 py-2">{formatNumber(value, 6)}</td>)}<td className="px-2 py-2">{formatNumber(atom.charge_e, 6)}</td><td className="px-2 py-2">{formatNumber(forceNorm, 6)}</td></tr>; })}</tbody></table></div>;
}

const TIMING_LABELS: Record<string, string> = {
  queue_wait_ms: "排队等待",
  structure_prepare_ms: "构象与结构准备",
  optimization_ms: "几何优化",
  gpu_wait_ms: "GPU 准入等待",
  model_load_ms: "模型加载 / 预热",
  model_compute_ms: "模型计算",
  hessian_ms: "Hessian",
  frequency_ms: "频率分析",
  artifact_ms: "产物序列化",
  total_ms: "总耗时"
};

const PROVENANCE_LABELS: Record<string, string> = {
  worker_version: "Worker 版本",
  worker_instance_id: "Worker 实例",
  model_id: "模型",
  model_alias: "模型别名",
  model_registry_key: "Registry key",
  model_family: "模型家族",
  model_reference: "模型参考",
  model_sha256: "模型 SHA-256",
  aimnet_version: "AIMNet 版本",
  aimnet_commit: "AIMNet commit",
  aimnet_wheel_sha256: "AIMNet wheel SHA-256",
  torch_version: "Torch",
  cuda_version: "CUDA",
  cuda_runtime: "CUDA runtime",
  warp_version: "Warp",
  gpu_name: "GPU",
  gpu_uuid: "GPU UUID",
  gpu_budget_mib: "GPU 预算 / MiB",
  gpu_active_thread_percentage: "MPS active-thread 上限 / %",
  gpu_preferred: "首选设备",
  gpu_physical_device: "物理设备",
  gpu_logical_device: "逻辑设备",
  logical_device: "逻辑设备",
  physical_gpu: "物理 GPU",
  visible_gpu_count: "可见 GPU 数",
  execution_path: "执行路径",
  broker_instance_id: "Broker 实例",
  lease_id: "GPU lease",
  fencing_token: "Broker fencing token",
  conformer_seed: "构象 seed",
  rdkit_version: "RDKit 版本",
  rdkit_force_field: "RDKit 构象方法",
  rdkit_optimization_performed: "RDKit 力场优化",
  rdkit_optimization_status: "RDKit 优化状态码",
  mass_source: "原子质量来源"
};

function provenanceDisplay(value: string | number | boolean | null | undefined): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value ?? "--");
}

function timingDisplay(key: string, value: number): string {
  if (key.endsWith("_ms")) return `${formatNumber(value, value < 10 ? 3 : 1)} ms`;
  if (key.endsWith("_seconds")) return `${formatNumber(value, 3)} s`;
  return formatNumber(value, 4);
}

function minimumAssessmentLabel(value: string): { label: string; className: string } {
  if (value === "confirmed_minimum") return { label: "确认极小值", className: "bg-emerald-50 text-emerald-700" };
  if (value === "nonminimum_or_saddle") return { label: "非极小值或鞍点", className: "bg-red-50 text-red-700" };
  if (value === "not_converged") return { label: "优化未收敛，无法判定", className: "bg-amber-50 text-amber-800" };
  return { label: "极小值未评估", className: "bg-slate-100 text-slate-600" };
}

export function MonomerDftResults({ job, onDeleteArtifacts, isDeletingArtifacts }: { job: MonomerDftJobResponse; onDeleteArtifacts: () => void; isDeletingArtifacts: boolean }) {
  const result = job.result;
  const artifactsState = effectiveMonomerDftArtifactsState(job);
  const [trajectory, setTrajectory] = useState<MonomerDftTrajectoryArtifact | null>(null);
  const [isTrajectoryLoading, setIsTrajectoryLoading] = useState(false);
  const [trajectoryError, setTrajectoryError] = useState<string | null>(null);
  const trajectoryArtifactId = result?.optimization?.trajectory_artifact_id ?? null;

  useEffect(() => {
    setTrajectory(null);
    setTrajectoryError(null);
    if (
      job.status !== "completed" ||
      !trajectoryArtifactId ||
      !job.artifacts.some((artifact) =>
        artifact.artifact_id === trajectoryArtifactId && isMonomerDftArtifactAvailable(job, artifact)
      )
    ) return;
    const controller = new AbortController();
    setIsTrajectoryLoading(true);
    fetchMonomerDftArtifactJson<MonomerDftTrajectoryArtifact>(job.job_id, trajectoryArtifactId, controller.signal)
      .then((payload) => setTrajectory(payload))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setTrajectoryError(error instanceof Error ? error.message : "优化轨迹载入失败。");
      })
      .finally(() => { if (!controller.signal.aborted) setIsTrajectoryLoading(false); });
    return () => controller.abort();
  }, [job.artifacts, job.artifacts_deleted, job.artifacts_state, job.job_id, job.status, trajectoryArtifactId]);

  const frames = useMemo(() => result ? buildFrames(result, trajectory) : [], [result, trajectory]);
  if (!result) {
    return <section className="rounded-2xl border border-dashed border-slate-300 bg-white p-10 text-center"><Gauge className="mx-auto h-7 w-7 text-slate-300" /><div className="mt-3 text-sm font-semibold text-slate-700">结果等待区</div><p className="mt-1 text-xs text-slate-500">任务完成后展示能量、显式坐标、原子属性、频率与产物。</p></section>;
  }
  const trace = result.optimization?.trace ?? [];
  const atoms = atomsForCoordinates(result, result.geometry.final_coordinates_angstrom, true);
  const frequencies = result.properties.frequencies;
  const lowestFrequency = frequencies ? lowestMonomerDftFrequency(frequencies.values_cm_1) : null;
  const hessian = result.properties.hessian;
  const assessment = minimumAssessmentLabel(result.scientific_status.minimum_assessment);
  const rdkitPreparation = resolveMonomerDftRdkitPreparation(result.rdkit);
  const timings = selectMonomerDftDisplayTimings(job, result);
  const hasAvailableArtifacts = hasAvailableMonomerDftArtifacts(job);
  const hasExplicitIsotopes = result.schema_version === 2 &&
    result.atoms.isotope_mass_numbers.some((massNumber) => massNumber > 0);
  const warnings = [...new Map(
    [...result.warnings, ...job.warnings.map((message) => ({ code: "job", message }))]
      .map((warning) => [warning.message, warning] as const)
  ).values()];

  return <div className="space-y-4">
    {warnings.length > 0 ? <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-900"><div className="flex items-start gap-2"><TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /><div>{warnings.map((warning, index) => <div key={`${warning.code}-${index}`}><span className="font-semibold">{warning.code}</span>：{warning.message}</div>)}</div></div></div> : null}
    {hasExplicitIsotopes ? <div className="rounded-xl border border-sky-200 bg-sky-50 p-3 text-xs leading-5 text-sky-900"><div className="flex items-start gap-2"><Info className="mt-0.5 h-4 w-4 shrink-0" /><div><div className="font-semibold">显式同位素质量已用于频率分析</div><div>同位素只改变质量相关的振动频率，不改变 AIMNet 势能面和电子能量。</div></div></div></div> : null}
    {rdkitPreparation.state === "not_performed" ? <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-950"><div className="flex items-start gap-2"><TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" /><div><div className="font-semibold">RDKit 构象：{result.rdkit.force_field}</div><div>未执行 MMFF/UFF 力场优化；AIMNet2 使用 ETKDG 嵌入坐标作为初始几何。</div></div></div></div> : null}
    {rdkitPreparation.state === "unknown" ? <div className="rounded-xl border border-sky-200 bg-sky-50 p-3 text-xs leading-5 text-sky-950"><div className="flex items-start gap-2"><Info className="mt-0.5 h-4 w-4 shrink-0" /><div><div className="font-semibold">RDKit 构象状态未知</div><div>这是缺少显式优化状态字段的旧版结果，现有记录不足以可靠判断力场优化是否执行或收敛。</div></div></div></div> : null}
    <Panel title="计算摘要" icon={<CheckCircle2 className="h-4 w-4 text-emerald-600" />}><Summary result={result} /></Panel>
    <MoleculeCoordinates3D frames={frames} />
    {isTrajectoryLoading ? <div className="flex items-center gap-2 rounded-xl border border-sky-100 bg-sky-50 p-3 text-xs text-sky-800"><Loader2 className="h-4 w-4 animate-spin" />正在从校验过的 optimization_trajectory 产物载入完整坐标帧…</div> : null}
    {trajectoryError ? <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">轨迹坐标载入失败，已降级为初始/最终结构：{trajectoryError}</div> : null}
    {trace.length > 0 ? <div className="grid gap-4 xl:grid-cols-2"><Panel title="优化能量" icon={<Gauge className="h-4 w-4 text-indigo-600" />}><LineChart points={trace.map((point: MonomerDftOptimizationStep) => ({ x: point.step, y: point.energy_eV }))} color="#4f46e5" yLabel="能量 / eV" /></Panel><Panel title="优化最大力" icon={<Gauge className="h-4 w-4 text-orange-600" />}><LineChart points={trace.map((point) => ({ x: point.step, y: point.fmax_eV_per_A }))} color="#f97316" yLabel="Fmax / eV Å⁻¹" /></Panel></div> : null}
    {frequencies ? <Panel title="振动频率" icon={<Gauge className="h-4 w-4 text-sky-600" />}><div className="mb-2 flex flex-wrap gap-2 text-xs"><span className="rounded-md bg-slate-100 px-2 py-1">虚频 {frequencies.imaginary_mode_count}</span><span className="rounded-md bg-slate-100 px-2 py-1">最低 {lowestFrequency == null ? "--" : formatNumber(lowestFrequency, 2)} cm⁻¹</span><span className={cnAssessment(assessment.className)}>{assessment.label}</span></div><p className="mb-2 text-[11px] leading-5 text-slate-500">极小值结论直接采用 Worker 的 scientific_status.minimum_assessment；单点任务即使没有虚频也保持“未评估”。</p><FrequencyChart values={frequencies.values_cm_1} imaginaryThresholdCm1={frequencies.imaginary_threshold_cm_1} /></Panel> : null}
    {hessian ? <Panel title="Hessian 摘要" icon={<TableProperties className="h-4 w-4 text-violet-600" />}><div className="grid gap-3 sm:grid-cols-3"><div className="rounded-lg bg-slate-50 p-3 text-xs"><div className="text-slate-500">矩阵形状</div><div className="mt-1 font-semibold text-slate-900">{hessian.shape[0]} × {hessian.shape[1]}</div></div><div className="rounded-lg bg-slate-50 p-3 text-xs"><div className="text-slate-500">最大对称误差</div><div className="mt-1 font-semibold text-slate-900">{formatNumber(hessian.symmetry_max_abs_eV_per_A2, 8)} eV/Å²</div></div><div className="rounded-lg bg-slate-50 p-3 text-xs"><div className="text-slate-500">对称性</div><div className="mt-1 font-semibold text-slate-900">{hessian.symmetric_within_tolerance ? "通过容差检查" : "未通过容差检查"}</div></div></div></Panel> : null}
    {atoms.length > 0 ? <Panel title="原子坐标、电荷与力" icon={<Atom className="h-4 w-4 text-cyan-600" />}><AtomTable atoms={atoms} /></Panel> : null}
    <div className="grid gap-4 xl:grid-cols-2">
      <Panel title="实际耗时" icon={<Timer className="h-4 w-4 text-sky-600" />}><div className="grid gap-2 sm:grid-cols-2">{Object.entries(timings).sort(([left], [right]) => left === "total_ms" ? 1 : right === "total_ms" ? -1 : left.localeCompare(right)).map(([key, value]) => <div key={key} className="flex justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2 text-xs"><span className="text-slate-500">{TIMING_LABELS[key] ?? key}</span><span className="font-mono font-semibold text-slate-900">{timingDisplay(key, value)}</span></div>)}</div><p className="mt-3 flex items-start gap-1.5 text-[11px] leading-5 text-slate-500"><Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />显示该任务真实分阶段计时，不用 smoke 组合耗时估算单项 SLA。</p></Panel>
      <Panel title="可复现性与运行环境" icon={<Info className="h-4 w-4 text-violet-600" />}><dl className="space-y-2">{Object.entries(result.provenance).map(([key, value]) => <div key={key} className="grid grid-cols-[120px_minmax(0,1fr)] gap-3 text-xs"><dt className="text-slate-500">{PROVENANCE_LABELS[key] ?? key}</dt><dd className="break-all font-mono text-slate-800">{provenanceDisplay(value)}</dd></div>)}</dl></Panel>
    </div>
    <Panel title="计算产物" icon={<FileArchive className="h-4 w-4 text-slate-600" />}>
      {artifactsState === "delete_requested" ? <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"><Loader2 className="h-3.5 w-3.5 animate-spin" />删除请求已接受，正在等待 Worker 清理并校验产物状态。</div> : null}
      {artifactsState === "deleted" ? <div className="mb-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">服务器产物已删除，任务元数据与校验摘要仍保留。</div> : null}
      {artifactsState === "none" ? <div className="mb-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">该任务没有生成可下载产物。</div> : null}
      <div className="space-y-2">
        {job.artifacts.length > 0 ? job.artifacts.map((artifact) => {
          const artifactAvailable = isMonomerDftArtifactAvailable(job, artifact);
          return <div key={artifact.artifact_id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-100 px-3 py-2"><div className="min-w-0"><div className="truncate text-xs font-semibold text-slate-900">{artifact.name}</div><div className="mt-0.5 text-[11px] text-slate-500">{artifact.media_type} · {formatBytes(artifact.size_bytes)} · SHA-256 {artifact.sha256.slice(0, 12)}…</div></div>{artifactAvailable ? <a href={getMonomerDftArtifactUrl(job.job_id, artifact.artifact_id)} download={artifact.name} className="inline-flex h-8 items-center rounded-md border border-slate-200 px-2.5 text-xs font-medium text-slate-700 hover:bg-slate-50"><Download className="mr-1.5 h-3.5 w-3.5" />下载</a> : <span aria-disabled="true" className="inline-flex h-8 items-center rounded-md border border-slate-200 px-2.5 text-xs font-medium text-slate-400"><Download className="mr-1.5 h-3.5 w-3.5" />不可用</span>}</div>;
        }) : <div className="text-xs text-slate-500">没有保留的产物。</div>}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {hasAvailableArtifacts ? <a href={getMonomerDftBundleUrl(job.job_id)} download className="inline-flex h-9 items-center rounded-md bg-slate-900 px-3 text-xs font-medium text-white hover:bg-slate-800"><FileArchive className="mr-1.5 h-3.5 w-3.5" />下载完整 ZIP</a> : <span aria-disabled="true" className="inline-flex h-9 items-center rounded-md bg-slate-900 px-3 text-xs font-medium text-white opacity-40"><FileArchive className="mr-1.5 h-3.5 w-3.5" />下载完整 ZIP</span>}
        <Button type="button" variant="outline" className="h-9 rounded-md border-red-200 px-3 text-xs text-red-700 hover:bg-red-50" disabled={isDeletingArtifacts || artifactsState === "delete_requested" || !hasAvailableArtifacts} onClick={onDeleteArtifacts}><Trash2 className="mr-1.5 h-3.5 w-3.5" />{isDeletingArtifacts || artifactsState === "delete_requested" ? "正在删除" : "删除服务器产物"}</Button>
      </div>
    </Panel>
  </div>;
}

function cnAssessment(className: string): string {
  return `rounded-md px-2 py-1 ${className}`;
}
