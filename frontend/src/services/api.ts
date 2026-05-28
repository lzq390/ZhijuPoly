import type {
  ConditionalGenerationJobCreateResponse,
  ConditionalGenerationJobStatusResponse,
  ConditionalGenerationTgRequest,
  DftEnergyStepBrowseResponse,
  DftMoleculeDetail,
  DftMoleculeBrowseResponse,
  DftPcaSampleResponse,
  ExperimentalProcessBrowseResponse,
  ExperimentalPropertyBrowseResponse,
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  LabDataProjectStats,
  LabDataSampleMeasurement,
  LabDataSampleMeasurementPage,
  LabDataSampleMeasurementPayload,
  LabDataSummary,
  LabDataTestProject,
  OnlineKnowledgeExportResponse,
  OnlineKnowledgeDefaultConfigResponse,
  OnlineKnowledgeHistoryResponse,
  OnlineKnowledgeJobCreateResponse,
  OnlineKnowledgeJobResponse,
  OnlineKnowledgeSearchRequest,
  OnlineKnowledgeSearchResponse,
  PredictRequest,
  PredictResponse,
  ReverseDesignTgJobCreateResponse,
  ReverseDesignTgJobStatusResponse,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse,
  SmilesLookupRequest,
  SmilesLookupResponse,
  StructureImageRecognitionResponse,
  SmilesQueryRequest,
  SmilesQueryResponse,
  StructurePropertyBrowseResponse
} from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

async function errorMessageFromResponse(response: Response): Promise<string> {
  const data = await response.json().catch(() => null);
  return typeof data?.detail === "string" ? data.detail : `Request failed with status ${response.status}`;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }

  return (await response.json()) as T;
}

async function postForm<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    body
  });

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }

  return (await response.json()) as T;
}

async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }

  return (await response.json()) as T;
}

export function querySmiles(payload: SmilesQueryRequest): Promise<SmilesQueryResponse> {
  return postJSON("/query/smiles", payload);
}

export function lookupSmilesInDatabase(payload: SmilesLookupRequest): Promise<SmilesLookupResponse> {
  return postJSON("/database-browser/smiles-lookup", payload);
}

export function predictSmiles(payload: PredictRequest): Promise<PredictResponse> {
  return postJSON("/predict", payload);
}

export function searchKnowledge(payload: KnowledgeSearchRequest): Promise<KnowledgeSearchResponse> {
  return postJSON("/knowledge/search", payload);
}

export function searchOnlineKnowledge(
  payload: OnlineKnowledgeSearchRequest
): Promise<OnlineKnowledgeSearchResponse> {
  return postJSON("/online-knowledge/search", payload);
}

export function createOnlineKnowledgeJob(
  payload: OnlineKnowledgeSearchRequest
): Promise<OnlineKnowledgeJobCreateResponse> {
  return postJSON("/online-knowledge/jobs", payload);
}

export function fetchOnlineKnowledgeDefaultConfig(): Promise<OnlineKnowledgeDefaultConfigResponse> {
  return getJSON("/online-knowledge/default-config");
}

export function fetchOnlineKnowledgeJob(jobId: string): Promise<OnlineKnowledgeJobResponse> {
  return getJSON(`/online-knowledge/jobs/${encodeURIComponent(jobId)}`);
}

export function fetchOnlineKnowledgeHistory(): Promise<OnlineKnowledgeHistoryResponse> {
  return getJSON("/online-knowledge/history");
}

export function clearOnlineKnowledgeHistory(): Promise<{ success: boolean }> {
  return postJSON("/online-knowledge/history/clear", {});
}

export async function deleteOnlineKnowledgeHistory(historyId: number): Promise<{ success: boolean }> {
  const response = await fetch(`${API_BASE_URL}/online-knowledge/history/${historyId}`, {
    method: "DELETE"
  });

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }

  return (await response.json()) as { success: boolean };
}

export function exportOnlineKnowledgeCsv(
  data: Record<string, unknown>[],
  filename?: string
): Promise<OnlineKnowledgeExportResponse> {
  return postJSON("/online-knowledge/export-csv", { data, filename });
}

export function searchReverseDesignByTg(payload: ReverseDesignTgRequest): Promise<ReverseDesignTgResponse> {
  return postJSON("/reverse-design/tg", payload);
}

export function createReverseDesignTgJob(
  payload: ReverseDesignTgRequest
): Promise<ReverseDesignTgJobCreateResponse> {
  return postJSON("/reverse-design/tg/jobs", payload);
}

export function fetchReverseDesignTgJob(jobId: string): Promise<ReverseDesignTgJobStatusResponse> {
  return getJSON(`/reverse-design/tg/jobs/${encodeURIComponent(jobId)}`);
}

export function createConditionalGenerationTgJob(
  payload: ConditionalGenerationTgRequest
): Promise<ConditionalGenerationJobCreateResponse> {
  return postJSON("/conditional-generation/tg/jobs", payload);
}

export function fetchConditionalGenerationTgJob(
  jobId: string
): Promise<ConditionalGenerationJobStatusResponse> {
  return getJSON(`/conditional-generation/tg/jobs/${encodeURIComponent(jobId)}`);
}

export function fetchStructure3D(
  smiles: string
): Promise<{ molblock: string; capped_smiles: string; format: "mol" }> {
  return postJSON("/structure/3d", { smiles });
}

export function recognizeStructureImage(file: File): Promise<StructureImageRecognitionResponse> {
  const body = new FormData();
  body.append("image", file);
  return postForm("/structure/recognize-image", body);
}

export function fetchDftPcaSample(limit = 200): Promise<DftPcaSampleResponse> {
  return getJSON(`/dft/pca-sample?limit=${limit}`);
}

export function fetchDftMolecule(molId: string): Promise<DftMoleculeDetail> {
  return getJSON(`/dft/molecule/${encodeURIComponent(molId)}`);
}

export function browseStructurePropertyRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}): Promise<StructurePropertyBrowseResponse> {
  const searchParams = new URLSearchParams();
  if (params.q) {
    searchParams.set("q", params.q);
  }
  if (params.page !== undefined) {
    searchParams.set("page", String(params.page));
  }
  if (params.page_size !== undefined) {
    searchParams.set("page_size", String(params.page_size));
  }

  const suffix = searchParams.toString() ? `?${searchParams.toString()}` : "";
  return getJSON(`/database-browser/structure-property${suffix}`);
}

function buildQueryString(params: { q?: string; mol_id?: string; page?: number; page_size?: number }): string {
  const searchParams = new URLSearchParams();
  if (params.q) {
    searchParams.set("q", params.q);
  }
  if (params.mol_id) {
    searchParams.set("mol_id", params.mol_id);
  }
  if (params.page !== undefined) {
    searchParams.set("page", String(params.page));
  }
  if (params.page_size !== undefined) {
    searchParams.set("page_size", String(params.page_size));
  }
  return searchParams.toString() ? `?${searchParams.toString()}` : "";
}

export function browseDftMolecules(params: {
  q?: string;
  page?: number;
  page_size?: number;
}): Promise<DftMoleculeBrowseResponse> {
  return getJSON(`/database-browser/dft/molecules${buildQueryString(params)}`);
}

export function browseDftEnergySteps(params: {
  q?: string;
  mol_id?: string;
  page?: number;
  page_size?: number;
}): Promise<DftEnergyStepBrowseResponse> {
  return getJSON(`/database-browser/dft/steps${buildQueryString(params)}`);
}

export function browseExperimentalProcessRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}): Promise<ExperimentalProcessBrowseResponse> {
  return getJSON(`/database-browser/experimental-process${buildQueryString(params)}`);
}

export function browseExperimentalPropertyRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}): Promise<ExperimentalPropertyBrowseResponse> {
  return getJSON(`/database-browser/experimental-property${buildQueryString(params)}`);
}

export function fetchLabDataTestProjects(): Promise<LabDataTestProject[]> {
  return getJSON("/lab-data/test-projects");
}

export function createLabDataSampleMeasurement(
  payload: LabDataSampleMeasurementPayload
): Promise<LabDataSampleMeasurement> {
  return postJSON("/lab-data/sample-measurements", payload);
}

export function fetchLabDataSampleMeasurements(params: {
  experimentProject?: string;
  page?: number;
  pageSize?: number;
  recentDays?: number;
}): Promise<LabDataSampleMeasurementPage> {
  const searchParams = new URLSearchParams();
  if (params.experimentProject) {
    searchParams.set("experiment_project", params.experimentProject);
  }
  if (params.page !== undefined) {
    searchParams.set("page", String(params.page));
  }
  if (params.pageSize !== undefined) {
    searchParams.set("page_size", String(params.pageSize));
  }
  if (params.recentDays !== undefined) {
    searchParams.set("recent_days", String(params.recentDays));
  }

  const suffix = searchParams.toString() ? `?${searchParams.toString()}` : "";
  return getJSON(`/lab-data/sample-measurements${suffix}`);
}

export function fetchLabDataSampleMeasurementCount(): Promise<{ count: number }> {
  return getJSON("/lab-data/sample-measurements/count");
}

export function fetchLabDataStatsByProject(): Promise<LabDataProjectStats[]> {
  return getJSON("/lab-data/sample-measurements/stats/by-project");
}

export function fetchLabDataSummary(): Promise<LabDataSummary> {
  return getJSON("/lab-data/sample-measurements/stats/summary");
}
