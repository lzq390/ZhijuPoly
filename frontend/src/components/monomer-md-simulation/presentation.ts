import type {
  MonomerMdArtifact,
  MonomerMdFormalProtocol,
  MonomerMdJobResponse,
  MonomerMdJobStatus,
  MonomerMdProtocol,
  MonomerMdSeries,
  MonomerMdSimulationResult,
  MonomerMdTrajectoryPoint,
  MonomerMdTrajectoryPreview,
  MonomerMdVisualizationStage
} from "../../types";
import { isRecord } from "./config";

export const JOB_STATUS_LABELS: Record<MonomerMdJobStatus, string> = {
  pending: "等待提交",
  submitted: "已排队",
  running: "运行中",
  cancel_requested: "取消中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

export const PROTOCOL_LABELS: Record<MonomerMdProtocol, string> = {
  DensityDemo: "DensityDemo",
  Density: "Density",
  Transport: "Transport",
  HVap: "HVap",
  Dielectric: "Dielectric",
  Compressibility: "Compressibility"
};

const METRIC_LABELS: Record<string, string> = {
  density: "密度",
  hvap: "汽化焓",
  dielectric: "介电常数",
  compressibility: "等温压缩率",
  viscosity: "粘度",
  volume: "体积",
  correlation_time: "相关时间",
  cubic_box_length: "立方盒长",
  conductivity_onsager: "Onsager 电导率",
  conductivity_NE: "Nernst–Einstein 电导率",
  conductivity_ne: "Nernst–Einstein 电导率",
  final_density_g_cm3: "最终密度",
  mean_density_g_cm3: "平均密度",
  mean_temperature_k: "平均温度",
  final_temperature_k: "最终温度",
  mean_total_energy_kcal_mol: "平均总能量",
  final_total_energy_kcal_mol: "最终总能量",
  elapsed_seconds: "耗时",
  n_atoms: "原子数",
  n_frames: "帧数",
  n_steps: "步数"
};

const PROTOCOL_METRICS: Record<MonomerMdFormalProtocol, string[]> = {
  Density: ["density"],
  HVap: ["density", "hvap"],
  Dielectric: ["dielectric", "volume", "correlation_time"],
  Compressibility: ["compressibility"],
  Transport: [
    "cubic_box_length",
    "conductivity_onsager",
    "conductivity_NE",
    "conductivity_ne",
    "viscosity"
  ]
};

const DEMO_SUMMARY_KEYS = [
  "final_density_g_cm3",
  "mean_density_g_cm3",
  "mean_temperature_k",
  "final_temperature_k",
  "mean_total_energy_kcal_mol",
  "final_total_energy_kcal_mol",
  "elapsed_seconds",
  "n_atoms",
  "n_frames",
  "n_steps"
];

const MESSAGE_TRANSLATIONS: Record<string, string> = {
  "Backend reports that monomer MD service is unavailable.": "后端报告单体 MD 服务当前不可用。",
  "Request validation failed with status 422": "请求参数校验失败，请检查输入。",
  "Request failed with status 422": "请求参数校验失败，请检查输入。",
  "monomer MD submissions are disabled": "单体 MD 提交功能当前已关闭。",
  "monomer MD worker is ready": "单体 MD Worker 已就绪。",
  "monomer MD worker is not configured": "单体 MD Worker 尚未配置。",
  "monomer MD worker is not reachable": "无法连接单体 MD Worker。",
  "monomer MD worker is draining for deployment": "单体 MD Worker 正在为部署排空任务。",
  "monomer MD worker is not accepting jobs": "单体 MD Worker 当前不接收新任务。",
  "monomer MD database capacity check failed": "无法读取单体 MD 任务容量。",
  "monomer MD job capacity is full; please wait for the active job to finish": "单体 MD 任务容量已满，请等待活跃任务结束。",
  "formal ByteFF2 monomer MD capacity is full; please wait for the current formal job to finish": "正式单体 MD 执行与排队容量已满。",
  "Failed to fetch": "网络请求失败。"
};

export type SeriesPoint = { x: number; y: number };

export type AdaptedMetric = {
  key: string;
  label: string;
  value: number | string | boolean;
  standardDeviation: number | null;
  unit: string | null;
};

export type TransportRow = {
  component: string;
  diffusivity: number | string | boolean;
  unit: string | null;
};

export type AdaptedMetrics = {
  metrics: AdaptedMetric[];
  transportRows: TransportRow[];
  contractWarnings: string[];
  raw: Record<string, unknown>;
};

export type AdaptedMonomerMdVisualization = {
  schemaVersion: 0 | 1 | 2 | 3;
  status: "complete" | "partial" | "unavailable";
  defaultStageId: string;
  stages: MonomerMdVisualizationStage[];
  warnings: string[];
};

export type SafeArtifact = {
  name: string;
  kind: string;
  sizeBytes: number | null;
  url: string | null;
};

export function clampMonomerMdProgress(value: unknown): number {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
}

export function formatNumber(value: number, digits = 3): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

export function formatMetricValue(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) {
    return formatNumber(value, Math.abs(value) >= 100 ? 1 : 4);
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string" && value.trim()) return value;
  return "--";
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "--";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

export function translateMonomerMdMessage(value: string | null | undefined): string | null {
  if (!value) return null;
  const message = value.trim();
  if (!message) return null;
  if (MESSAGE_TRANSLATIONS[message]) return MESSAGE_TRANSLATIONS[message];
  if (message.startsWith("invalid smiles:")) return "SMILES 无法解析，请检查结构格式。";
  const requestStatus = /^Request (?:validation )?failed with status (\d+)$/.exec(message);
  if (requestStatus) return `请求失败（HTTP ${requestStatus[1]}），请稍后重试。`;
  if (message.includes("single-molecule SMILES without attachment points")) {
    return "单体 MD 只接受不含 * 连接点的普通单分子 SMILES。";
  }
  if (message.startsWith("monomer MD worker health check failed:")) {
    return `单体 MD Worker 健康检查失败：${message.slice(message.indexOf(":") + 1).trim()}`;
  }
  if (message.startsWith("monomer MD worker runtime is not ready:")) {
    return `单体 MD Worker 运行环境尚未就绪：${message.slice(message.indexOf(":") + 1).trim()}`;
  }
  return message;
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function normalizeMonomerMdSeries(
  series: MonomerMdSeries | undefined,
  valueKeys: string[]
): SeriesPoint[] {
  if (!series) return [];
  const rawPoints = Array.isArray(series) ? series : series.points;
  if (!Array.isArray(rawPoints)) return [];
  return rawPoints
    .map((raw, index) => {
      if (!isRecord(raw)) return null;
      const timeNs = numberValue(raw.time_ns);
      const x =
        numberValue(raw.time_ps) ??
        (timeNs == null ? null : timeNs * 1000) ??
        numberValue(raw.step) ??
        numberValue(raw.frame) ??
        index;
      const y =
        numberValue(raw.value) ??
        valueKeys.map((key) => numberValue(raw[key])).find((item) => item != null) ??
        Object.entries(raw)
          .filter(([key]) => !["time_ps", "time_ns", "step", "frame"].includes(key))
          .map(([, value]) => numberValue(value))
          .find((item) => item != null) ??
        null;
      return y == null ? null : { x, y };
    })
    .filter((point): point is SeriesPoint => point !== null);
}

function visualizationStageHasData(stage: MonomerMdVisualizationStage): boolean {
  const series = [
    stage.density_series,
    stage.temperature_series,
    stage.potential_energy_series,
    stage.kinetic_energy_series,
    stage.energy_series
  ];
  const hasSeries = series.some((item) => {
    if (!item) return false;
    return Array.isArray(item) ? item.length > 0 : Array.isArray(item.points) && item.points.length > 0;
  });
  return hasSeries
    || monomerMdTrajectoryPreviewPoints(stage.trajectory_preview).length > 0
    || Boolean(stage.trajectory_timeline?.frames?.length && stage.trajectory_timeline?.atoms?.length)
    || Boolean(stage.trajectory_timeline_available);
}

function stringWarnings(value: unknown): string[] {
  return Array.isArray(value)
    ? [...new Set(value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())))]
    : [];
}

export function adaptMonomerMdVisualization(
  result: MonomerMdSimulationResult | null
): AdaptedMonomerMdVisualization {
  const nested = result?.visualization;
  if (
    (nested?.schema_version === 1 || nested?.schema_version === 2 || nested?.schema_version === 3)
    && Array.isArray(nested.stages)
    && nested.stages.length > 0
  ) {
    const seen = new Set<string>();
    const stages = nested.stages.filter((stage) => {
      if (!stage || typeof stage.stage_id !== "string" || !stage.stage_id.trim() || seen.has(stage.stage_id)) return false;
      seen.add(stage.stage_id);
      return true;
    }).map((stage) => ({
      ...stage,
      label: typeof stage.label === "string" && stage.label.trim() ? stage.label : stage.stage_id,
      warnings: stringWarnings(stage.warnings)
    }));
    if (stages.length > 0) {
      const declared = stages.find((stage) => stage.stage_id === nested.default_stage_id);
      const defaultStage = declared && visualizationStageHasData(declared)
        ? declared
        : stages.find(visualizationStageHasData) ?? stages[0];
      return {
        schemaVersion: nested.schema_version,
        status: nested.status === "complete" || nested.status === "partial" || nested.status === "unavailable"
          ? nested.status
          : stages.some(visualizationStageHasData) ? "partial" : "unavailable",
        defaultStageId: defaultStage.stage_id,
        stages,
        warnings: stringWarnings(nested.warnings)
      };
    }
  }

  const legacyStage: MonomerMdVisualizationStage = {
    stage_id: result?.trajectory_preview?.stage_id || "npt",
    label: "结果阶段",
    density_series: result?.density_series,
    temperature_series: result?.temperature_series,
    potential_energy_series: result?.potential_energy_series,
    kinetic_energy_series: result?.kinetic_energy_series,
    energy_series: result?.energy_series,
    trajectory_preview: result?.trajectory_preview ?? null,
    warnings: stringWarnings(result?.warnings)
  };
  const hasData = visualizationStageHasData(legacyStage);
  return {
    schemaVersion: 0,
    status: hasData ? (legacyStage.warnings?.length ? "partial" : "complete") : "unavailable",
    defaultStageId: legacyStage.stage_id,
    stages: [legacyStage],
    warnings: legacyStage.warnings ?? []
  };
}

const VISUALIZATION_WARNING_LABELS: Record<string, string> = {
  STATE_CSV_MISSING: "该阶段缺少状态 CSV，无法生成热力学曲线。",
  STATE_CSV_UNSAFE: "该阶段状态 CSV 未通过安全校验。",
  STATE_CSV_UNREADABLE: "该阶段状态 CSV 无法解析。",
  STATE_CSV_EMPTY: "该阶段状态 CSV 中没有可用数值。",
  DENSITY_SERIES_UNAVAILABLE: "该阶段没有可用密度序列。",
  TEMPERATURE_SERIES_UNAVAILABLE: "该阶段没有可用温度序列。",
  POTENTIAL_ENERGY_SERIES_UNAVAILABLE: "该阶段没有可用势能序列。",
  KINETIC_ENERGY_SERIES_UNAVAILABLE: "该阶段没有可用动能序列。",
  ENERGY_SERIES_UNAVAILABLE: "该阶段没有可用能量序列。",
  TRAJECTORY_TOPOLOGY_MISSING: "该阶段缺少构象拓扑文件。",
  TRAJECTORY_TOPOLOGY_UNSAFE: "该阶段构象拓扑未通过安全校验。",
  TRAJECTORY_TOPOLOGY_UNREADABLE: "该阶段构象拓扑无法解析。",
  TRAJECTORY_DCD_MISSING: "该阶段缺少轨迹文件。",
  TRAJECTORY_DCD_UNSAFE: "该阶段轨迹文件未通过安全校验。",
  TRAJECTORY_DCD_UNREADABLE: "该阶段轨迹文件无法解析。",
  TRAJECTORY_READER_UNAVAILABLE: "Worker 缺少轨迹读取运行时。",
  TRAJECTORY_EMPTY: "该阶段轨迹不包含帧。",
  TRAJECTORY_ATOM_COUNT_MISMATCH: "轨迹与拓扑的原子数量不一致。",
  TRAJECTORY_COORDINATES_NONFINITE: "最终帧包含无效坐标。",
  TRAJECTORY_BOX_UNAVAILABLE: "该阶段没有可用周期盒信息。",
  TRAJECTORY_TIME_ALIGNMENT_UNAVAILABLE: "轨迹帧与状态时间无法可靠对齐，时间轴仅显示真实帧号。",
  TRAJECTORY_TIMELINE_ENCODING_UNAVAILABLE: "该阶段无法生成有界的轨迹时间轴，仍可查看最终帧。",
  VISUALIZATION_EXTRACTION_FAILED: "正式结果可视化后处理失败，科学指标仍然有效。"
};

export function monomerMdVisualizationWarningText(warning: string): string {
  const code = warning.includes(":") ? warning.slice(warning.lastIndexOf(":") + 1) : warning;
  return VISUALIZATION_WARNING_LABELS[code] ?? warning;
}

function scalar(value: unknown): value is number | string | boolean {
  return (
    (typeof value === "number" && Number.isFinite(value)) ||
    typeof value === "string" ||
    typeof value === "boolean"
  );
}

function unitFor(
  key: string,
  source: Record<string, unknown>,
  summary: Record<string, unknown>,
  units: Record<string, unknown>
): string | null {
  const candidates = [
    source[`${key}_unit`],
    summary[`${key}_unit`],
    units[key],
    key === "Dself_inf" ? source.diffusivity_unit : undefined
  ];
  return candidates.find((value): value is string => typeof value === "string" && Boolean(value.trim())) ?? null;
}

export function adaptMonomerMdMetrics(
  result: MonomerMdSimulationResult,
  protocol: MonomerMdProtocol
): AdaptedMetrics {
  const metricsSource = isRecord(result.metrics) ? result.metrics : {};
  const summary = isRecord(result.summary) ? result.summary : {};
  const source = { ...summary, ...metricsSource };
  const units = isRecord(metricsSource.units) ? metricsSource.units : {};
  const consumed = new Set<string>(["units"]);
  const keys = protocol === "DensityDemo"
    ? DEMO_SUMMARY_KEYS
    : PROTOCOL_METRICS[protocol as MonomerMdFormalProtocol] ?? [];
  const cards: AdaptedMetric[] = [];
  for (const key of keys) {
    const value = source[key];
    if (!scalar(value)) continue;
    const stdKey = `${key}_std`;
    cards.push({
      key,
      label: METRIC_LABELS[key] ?? key,
      value,
      standardDeviation: numberValue(source[stdKey]),
      unit: unitFor(key, metricsSource, summary, units)
    });
    consumed.add(key);
    consumed.add(stdKey);
    consumed.add(`${key}_unit`);
  }

  const transportRows: TransportRow[] = [];
  const contractWarnings: string[] = [];
  if (protocol === "Transport") {
    consumed.add("components");
    consumed.add("Dself_inf");
    consumed.add("diffusivity_unit");
    const components = Array.isArray(source.components) ? source.components : null;
    const diffusivities = Array.isArray(source.Dself_inf) ? source.Dself_inf : null;
    if ("components" in source || "Dself_inf" in source) {
      if (!components || !diffusivities || components.length !== diffusivities.length) {
        contractWarnings.push("Transport 返回的 components 与 Dself_inf 长度不一致，无法可靠配对。 ");
      } else {
        for (let index = 0; index < components.length; index += 1) {
          const component = components[index];
          const diffusivity = diffusivities[index];
          if (typeof component === "string" && scalar(diffusivity)) {
            transportRows.push({
              component,
              diffusivity,
              unit: unitFor("Dself_inf", metricsSource, summary, units)
            });
          } else {
            contractWarnings.push("Transport 自扩散结果包含无法识别的组分或数值。 ");
            break;
          }
        }
      }
    }
  }

  const raw = Object.fromEntries(
    Object.entries(source).filter(([key]) => {
      if (consumed.has(key) || key.endsWith("_unit")) return false;
      return !cards.some((metric) => metric.key === key || `${metric.key}_std` === key);
    })
  );

  if (contractWarnings.length > 0) {
    raw.components = source.components;
    raw.Dself_inf = source.Dself_inf;
  }
  return {
    metrics: cards,
    transportRows,
    contractWarnings: [...new Set(contractWarnings.map((item) => item.trim()))],
    raw
  };
}

function safeUrl(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  return /^(https?:\/\/|\/)/i.test(value.trim()) ? value.trim() : null;
}

function basename(value: string): string {
  const normalized = value.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).pop() ?? "输出文件";
}

function artifactFromValue(key: string, value: unknown): SafeArtifact | null {
  if (isRecord(value)) {
    const artifact = value as MonomerMdArtifact;
    const fallbackPath = typeof artifact.path === "string" ? basename(artifact.path) : key;
    const rawName =
      (typeof artifact.name === "string" && artifact.name.trim()) ||
      (typeof artifact.label === "string" && artifact.label.trim()) ||
      fallbackPath;
    return {
      name: basename(rawName),
      kind:
        (typeof artifact.kind === "string" && artifact.kind.trim()) ||
        basename(fallbackPath).split(".").pop()?.toUpperCase() ||
        "文件",
      sizeBytes:
        typeof artifact.size_bytes === "number" && Number.isFinite(artifact.size_bytes)
          ? artifact.size_bytes
          : null,
      url: safeUrl(artifact.url)
    };
  }
  if (typeof value === "string") {
    return {
      name: key || basename(value),
      kind: basename(key || value).split(".").pop()?.toUpperCase() || "文件",
      sizeBytes: null,
      url: safeUrl(value)
    };
  }
  return null;
}

export function safeMonomerMdArtifacts(
  result: MonomerMdSimulationResult | null,
  job: MonomerMdJobResponse | null
): SafeArtifact[] {
  const raw = result?.artifacts ?? job?.artifacts ?? [];
  if (Array.isArray(raw)) {
    return raw
      .map((artifact, index) => artifactFromValue(`输出文件 ${index + 1}`, artifact))
      .filter((artifact): artifact is SafeArtifact => artifact !== null);
  }
  if (isRecord(raw)) {
    return Object.entries(raw)
      .map(([key, value]) => artifactFromValue(key, value))
      .filter((artifact): artifact is SafeArtifact => artifact !== null);
  }
  return [];
}

export function monomerMdTrajectoryPoints(
  result: MonomerMdSimulationResult | null
): MonomerMdTrajectoryPoint[] {
  return monomerMdTrajectoryPreviewPoints(result?.trajectory_preview);
}

export function monomerMdTrajectoryPreviewPoints(
  preview: MonomerMdTrajectoryPreview | null | undefined
): MonomerMdTrajectoryPoint[] {
  if (!preview) return [];
  const points = Array.isArray(preview.points)
    ? preview.points
    : Array.isArray(preview.atoms)
      ? preview.atoms
      : [];
  return points.filter(
    (point) =>
      Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.z)
  );
}
