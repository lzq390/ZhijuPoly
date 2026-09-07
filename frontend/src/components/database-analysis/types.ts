import type { DatasetSummaryItem } from "../../types";

export type DatasetKey = "process" | "property" | "structureEffect" | "dft" | "formulation";
export type AnalysisViewKey = "overview" | DatasetKey;
export type DftTabKey = "analysis" | "records" | "steps";

export type RankedItem = {
  label: string;
  value: number;
  color?: string;
};

export type RangeItem = {
  label: string;
  count: number;
  min: number;
  median: number;
  max: number;
  p5?: number;
  p95?: number;
};

export type NumericRange = Omit<RangeItem, "label"> & { label?: string };

export type HistogramBin = {
  start: number;
  end: number;
  value: number;
};

export type OrbitalDistribution = {
  label: string;
  color: string;
  count: number;
  min: number;
  p5: number;
  median: number;
  p95: number;
  max: number;
  bins: HistogramBin[];
};

export type ProcessAnalytics = {
  rows: number;
  uniqueRecordIds: number;
  uniquePolymers: number;
  uniqueProducts: number;
  avgProcessTextLength: number;
  processSignalSummary: {
    extractedRows: number;
    uniqueSnippets: number;
    medianChars: number;
  };
  processSignals: Array<RankedItem & { total: number }>;
  topTerms: RankedItem[];
  topProducts: RankedItem[];
  topMaterials: RankedItem[];
};

export type PropertyAnalytics = {
  rows: number;
  uniquePolymers: number;
  uniqueProperties: number;
  categories: RankedItem[];
  topProperties: RankedItem[];
  ranges: RangeItem[];
  categoryTop: RankedItem[];
};

export type StructureEffectAnalytics = {
  rows: number;
  uniqueSmiles: number;
  properties: RankedItem[];
  units: RankedItem[];
  sources: RankedItem[];
  sourceMatrix: Array<{
    label: string;
    exp: number;
    sim: number;
    na: number;
  }>;
  ranges: RangeItem[];
};

export type DftAnalytics = {
  rows: number;
  molCount: number;
  energyRange: NumericRange;
  gapRange: NumericRange;
  orbitalDistributions: OrbitalDistribution[];
  stepRange: NumericRange;
  atomRange: NumericRange;
  atomTotals: RankedItem[];
  convergence: RankedItem[];
};

export type FormulationAnalytics = {
  files: number;
  rows: number;
  coverage: Array<{ label: string; count: number; pct: number }>;
  componentCounts: RankedItem[];
  topComponents: RankedItem[];
  polymerFamilies: RankedItem[];
  ratioTypes: RankedItem[];
  tempBands: RankedItem[];
  timeUnits: RankedItem[];
  topCatalysts: RankedItem[];
  topSolvents: RankedItem[];
  examples: Array<{
    title: string;
    polymer: string;
    formula: string;
    condition: string;
  }>;
};

export type DatabaseAnalyticsPayload = Partial<{
  process: ProcessAnalytics;
  property: PropertyAnalytics;
  structureEffect: StructureEffectAnalytics;
  dft: DftAnalytics;
  formulation: FormulationAnalytics;
}>;

export type DatasetDefinition = {
  key: DatasetKey;
  routeKey: string;
  title: string;
  subtitle: string;
  description: string;
  accent: string;
  soft: string;
};

export type DisplayDataset = DatasetDefinition & {
  recordCount: number | null;
  sourceStatus: string;
  sourceMessage: string | null;
  dataSource: string | null;
  latestImportStatus: string | null;
  latestImportFinishedAt: string | null;
};

export type DrawerMode = "records" | "dftSteps";

export type DrawerRequest = {
  dataset: DatasetKey;
  context: string;
  query?: string;
  mode?: DrawerMode;
  molId?: string;
};

export type AnalyticsValidationErrors = Partial<Record<DatasetKey, string>>;

export type DftConvergenceTone = "success" | "warning" | "danger" | "neutral";

export type DftConvergencePresentation = {
  label: string;
  tone: DftConvergenceTone;
};

export function dftConvergencePresentation(value: string | null | undefined): DftConvergencePresentation {
  const label = value?.trim() || "—";
  const normalized = value?.trim().toLowerCase().replace(/[\s-]+/g, "_") ?? "";
  if (["converged", "true", "yes", "1", "已收敛"].includes(normalized)) {
    return { label, tone: "success" };
  }
  if (["not_converged", "unconverged", "false", "no", "0", "failed", "未收敛"].includes(normalized)) {
    return { label, tone: "danger" };
  }
  if (["max_steps", "max_steps_reached", "maximum_steps_reached", "达到最大步数"].includes(normalized)) {
    return { label, tone: "warning" };
  }
  return { label, tone: "neutral" };
}

const DATASET_KEYS: DatasetKey[] = ["process", "property", "structureEffect", "dft", "formulation"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value);
}

function hasNumbers(value: Record<string, unknown>, keys: string[]) {
  return keys.every((key) => isFiniteNumber(value[key]));
}

function isRankedList(value: unknown) {
  return Array.isArray(value) && value.every((item) =>
    isRecord(item) && typeof item.label === "string" && isFiniteNumber(item.value)
  );
}

function isRange(value: unknown, requireLabel = true) {
  return isRecord(value)
    && (!requireLabel || typeof value.label === "string")
    && hasNumbers(value, ["count", "min", "median", "max"]);
}

function isRangeList(value: unknown) {
  return Array.isArray(value) && value.every((item) => isRange(item));
}

function validateProcess(value: unknown): value is ProcessAnalytics {
  if (!isRecord(value) || !hasNumbers(value, ["rows", "uniqueRecordIds", "uniquePolymers", "uniqueProducts", "avgProcessTextLength"])) return false;
  const summary = value.processSignalSummary;
  return isRecord(summary)
    && hasNumbers(summary, ["extractedRows", "uniqueSnippets", "medianChars"])
    && isRankedList(value.processSignals)
    && isRankedList(value.topTerms)
    && isRankedList(value.topProducts)
    && isRankedList(value.topMaterials);
}

function validateProperty(value: unknown): value is PropertyAnalytics {
  return isRecord(value)
    && hasNumbers(value, ["rows", "uniquePolymers", "uniqueProperties"])
    && isRankedList(value.categories)
    && isRankedList(value.topProperties)
    && isRangeList(value.ranges)
    && isRankedList(value.categoryTop);
}

function validateStructureEffect(value: unknown): value is StructureEffectAnalytics {
  return isRecord(value)
    && hasNumbers(value, ["rows", "uniqueSmiles"])
    && isRankedList(value.properties)
    && isRankedList(value.units)
    && isRankedList(value.sources)
    && Array.isArray(value.sourceMatrix)
    && value.sourceMatrix.every((item) => isRecord(item)
      && typeof item.label === "string"
      && hasNumbers(item, ["exp", "sim", "na"]))
    && isRangeList(value.ranges);
}

function validateDft(value: unknown): value is DftAnalytics {
  return isRecord(value)
    && hasNumbers(value, ["rows", "molCount"])
    && isRange(value.energyRange, false)
    && isRange(value.gapRange, false)
    && isRange(value.stepRange, false)
    && isRange(value.atomRange, false)
    && isRankedList(value.atomTotals)
    && isRankedList(value.convergence)
    && Array.isArray(value.orbitalDistributions)
    && value.orbitalDistributions.every((item) => isRecord(item)
      && typeof item.label === "string"
      && typeof item.color === "string"
      && hasNumbers(item, ["count", "min", "p5", "median", "p95", "max"])
      && Array.isArray(item.bins)
      && item.bins.every((bin) => isRecord(bin) && hasNumbers(bin, ["start", "end", "value"])));
}

function validateFormulation(value: unknown): value is FormulationAnalytics {
  return isRecord(value)
    && hasNumbers(value, ["files", "rows"])
    && Array.isArray(value.coverage)
    && value.coverage.every((item) => isRecord(item)
      && typeof item.label === "string"
      && hasNumbers(item, ["count", "pct"]))
    && ["componentCounts", "topComponents", "polymerFamilies", "ratioTypes", "tempBands", "timeUnits", "topCatalysts", "topSolvents"]
      .every((key) => isRankedList(value[key]))
    && Array.isArray(value.examples)
    && value.examples.every((item) => isRecord(item)
      && ["title", "polymer", "formula", "condition"].every((key) => typeof item[key] === "string"));
}

const DATASET_VALIDATORS: Record<DatasetKey, (value: unknown) => boolean> = {
  process: validateProcess,
  property: validateProperty,
  structureEffect: validateStructureEffect,
  dft: validateDft,
  formulation: validateFormulation
};

export function validateDatabaseAnalyticsPayload(raw: unknown): {
  analytics: DatabaseAnalyticsPayload;
  errors: AnalyticsValidationErrors;
} {
  const analytics: DatabaseAnalyticsPayload = {};
  const errors: AnalyticsValidationErrors = {};
  if (!isRecord(raw)) {
    for (const key of DATASET_KEYS) errors[key] = "分析数据格式异常，暂时无法读取该数据集。";
    return { analytics, errors };
  }
  for (const key of DATASET_KEYS) {
    const value = raw[key];
    if (value === undefined || value === null) continue;
    if (!DATASET_VALIDATORS[key](value)) {
      errors[key] = "分析数据不完整或格式异常，请重新加载。";
      continue;
    }
    Object.assign(analytics, { [key]: value });
  }
  return { analytics, errors };
}

export function isDatasetReady(dataset: DisplayDataset) {
  return dataset.sourceStatus === "ready";
}

export function toDisplayDataset(
  definition: DatasetDefinition,
  summary: DatasetSummaryItem | undefined,
  summaryLoading: boolean,
  summaryError: string | null,
  analyticsRecordCount?: number
): DisplayDataset {
  if (summary) {
    return {
      ...definition,
      recordCount: summary.total_records,
      sourceStatus: summary.source_status,
      sourceMessage: summary.source_message,
      dataSource: summary.data_source,
      latestImportStatus: summary.latest_import_status,
      latestImportFinishedAt: summary.latest_import_finished_at
    };
  }

  return {
    ...definition,
    recordCount: analyticsRecordCount ?? null,
    sourceStatus: summaryLoading ? "loading" : summaryError && analyticsRecordCount === undefined ? "unavailable" : "unknown",
    sourceMessage: summaryError,
    dataSource: null,
    latestImportStatus: null,
    latestImportFinishedAt: null
  };
}
