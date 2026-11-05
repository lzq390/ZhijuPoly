import type { ActiveModule } from "../routing";

export type OverviewSource<T> = {
  status: "ready" | "partial" | "unavailable";
  data: T | null;
  message?: string;
};

export type OverviewTask = {
  id: string;
  jobId: string;
  source: "monomerMd" | "monomerDft" | "polymerMd" | "polymerizationBatch";
  title: string;
  module: ActiveModule;
  status: string;
  statusLabel: string;
  outcome: "active" | "completed" | "attention" | "cancelled" | "unknown";
  progress: number | null;
  stage: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  finishedAt: string | null;
};

export type OverviewTaskSource = {
  id: OverviewTask["source"];
  label: string;
  status: "ready" | "partial" | "unavailable";
  total: number | null;
  tasks: OverviewTask[];
  message?: string;
};

export type OverviewStructure = { id: string; name: string; createdAt: string | null; updatedAt: string | null };
export type OverviewKnowledge = {
  sources: Array<{ id: string; label: string; ready: boolean; enabled: boolean; total: number | null }>;
};
export type OverviewKnowledgeActivity = {
  dailyAvailable: boolean;
  recentAvailable: boolean;
  daily: Array<{ date: string; count: number }>;
  recent: Array<{ id: string; title: string; status: string; statusLabel: string; submittedAt: string | null; updatedAt: string | null }>;
  warnings: string[];
};

export type OverviewData = {
  asOf: string;
  structures: OverviewSource<OverviewStructure[]>;
  taskSources: OverviewTaskSource[];
  knowledge: OverviewSource<OverviewKnowledge>;
  knowledgeActivity: OverviewSource<OverviewKnowledgeActivity>;
};

export type OverviewDataState = {
  data: OverviewData | null;
  access: "member" | "guest" | "restricted" | "checking";
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  refresh: () => void;
};
