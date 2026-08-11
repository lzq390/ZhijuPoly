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
  energyRange: RangeItem;
  gapRange: RangeItem;
  orbitalDistributions: OrbitalDistribution[];
  stepRange: RangeItem;
  atomRange: RangeItem;
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

export function isDatasetReady(dataset: DisplayDataset) {
  return dataset.sourceStatus === "ready";
}

export function toDisplayDataset(
  definition: DatasetDefinition,
  summary: DatasetSummaryItem | undefined,
  summaryLoading: boolean,
  summaryError: string | null
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
    recordCount: null,
    sourceStatus: summaryLoading ? "loading" : summaryError ? "unavailable" : "unknown",
    sourceMessage: summaryError,
    dataSource: null,
    latestImportStatus: null,
    latestImportFinishedAt: null
  };
}
