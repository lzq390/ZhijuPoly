import type {
  AssistantChatStreamRequest,
  AssistantSkillErrorEvent,
  AssistantSkillResultEvent,
  AssistantSkillStartEvent,
  ConditionalGenerationJobCreateResponse,
  ConditionalGenerationJobStatusResponse,
  ConditionalGenerationTgRequest,
  ConditionalGenerationTgStatusResponse,
  DatabaseAnalyticsResponse,
  DatasetSummaryResponse,
  DftEnergyStepBrowseResponse,
  DftMoleculeDetail,
  DftMoleculeBrowseResponse,
  DftPcaSampleResponse,
  ExperimentalProcessBrowseResponse,
  ExperimentalPropertyBrowseResponse,
  FormulationBrowseResponse,
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  LabDataProjectStats,
  LabDataSampleMeasurement,
  LabDataSampleMeasurementPage,
  LabDataSampleMeasurementPayload,
  LabDataSummary,
  LabDataTestProject,
  MdDemoAtomDistanceRequest,
  MdDemoAtomDistanceResponse,
  MdDemoDefaultsResponse,
  MdDemoRunRequest,
  MdDemoRunResponse,
  MonomerMdJobCreateRequest,
  MonomerMdJobCreateResponse,
  MonomerMdJobResponse,
  MonomerMdProtocolCatalogResponse,
  MonomerMdServiceStatusResponse,
  MonomerPolymerizationRequest,
  MonomerPolymerizationResponse,
  MonomerPolymerizationStatusResponse,
  MonomerRetrosynthesisRequest,
  MonomerRetrosynthesisResponse,
  OnlineKnowledgeExportResponse,
  OnlineKnowledgeDefaultConfigResponse,
  OnlineKnowledgeHistoryResponse,
  OnlineKnowledgeJobCreateResponse,
  OnlineKnowledgeJobResponse,
  OnlineKnowledgeSearchRequest,
  OnlineKnowledgeSearchResponse,
  PolytaoDescriptorRequest,
  PolytaoDescriptorResponse,
  PolytaoGenerationRequest,
  PolytaoJobCreateResponse,
  PolytaoJobStatusResponse,
  PolytaoStatusResponse,
  PredictRequest,
  PredictResponse,
  PropertyFilterOptionsResponse,
  PropertyFilterSearchRequest,
  PropertyFilterSearchResponse,
  ReverseDesignTgJobCreateResponse,
  ReverseDesignTgJobStatusResponse,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse,
  SmilesLookupRequest,
  SmilesLookupResponse,
  SmilesStandardizeRequest,
  SmilesStandardizeResponse,
  StructureImageRecognitionResponse,
  SmilesQueryRequest,
  SmilesQueryResponse,
  StructurePropertyBrowseResponse
} from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export class ApiRequestError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

export function isApiRequestError(error: unknown, status?: number): error is ApiRequestError {
  return error instanceof ApiRequestError && (status === undefined || error.status === status);
}

async function errorMessageFromResponse(response: Response): Promise<string> {
  const data = await response.json().catch(() => null);
  if (typeof data?.detail === "string") {
    return data.detail;
  }
  if (Array.isArray(data?.detail)) {
    return `Request validation failed with status ${response.status}`;
  }
  return `Request failed with status ${response.status}`;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    throw new ApiRequestError(response.status, await errorMessageFromResponse(response));
  }

  return (await response.json()) as T;
}

async function postForm<T>(path: string, body: FormData, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    body,
    signal
  });

  if (!response.ok) {
    throw new ApiRequestError(response.status, await errorMessageFromResponse(response));
  }

  return (await response.json()) as T;
}


export type AssistantStreamHandlers = {
  signal?: AbortSignal;
  onToken: (token: string) => void;
  onDone?: (message: string) => void;
  onError?: (detail: string) => void;
  onSkillStart?: (payload: AssistantSkillStartEvent) => void;
  onSkillResult?: (payload: AssistantSkillResultEvent) => void;
  onSkillError?: (payload: AssistantSkillErrorEvent) => void;
};

export async function streamAssistantChat(
  payload: AssistantChatStreamRequest,
  handlers: AssistantStreamHandlers
): Promise<void> {
  const response = await fetch(API_BASE_URL + "/assistant/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: handlers.signal
  });

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }
  if (!response.body) {
    throw new Error("Assistant response stream is not available");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      handleAssistantStreamEvent(part, handlers);
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) {
    handleAssistantStreamEvent(buffer, handlers);
  }
}

export async function streamAssistantImageChat(
  payload: AssistantChatStreamRequest,
  image: File,
  handlers: AssistantStreamHandlers
): Promise<void> {
  const body = new FormData();
  body.append("payload", JSON.stringify(payload));
  body.append("image", image);

  const response = await fetch(API_BASE_URL + "/assistant/chat/image-stream", {
    method: "POST",
    body,
    signal: handlers.signal
  });

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }
  if (!response.body) {
    throw new Error("Assistant image response stream is not available");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      handleAssistantStreamEvent(part, handlers);
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) {
    handleAssistantStreamEvent(buffer, handlers);
  }
}

function handleAssistantStreamEvent(rawEvent: string, handlers: AssistantStreamHandlers): void {
  const lines = rawEvent.split(/\r?\n/);
  let eventName = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (!dataLines.length) {
    return;
  }

  const data = JSON.parse(dataLines.join("\n")) as { content?: string; message?: string; detail?: string };
  if (eventName === "token" && data.content) {
    handlers.onToken(data.content);
  } else if (eventName === "done") {
    handlers.onDone?.(data.message ?? "");
  } else if (eventName === "skill_start") {
    handlers.onSkillStart?.(data as AssistantSkillStartEvent);
  } else if (eventName === "skill_result") {
    handlers.onSkillResult?.(data as AssistantSkillResultEvent);
  } else if (eventName === "skill_error") {
    handlers.onSkillError?.(data as AssistantSkillErrorEvent);
  } else if (eventName === "error") {
    const detail = data.detail ?? "Assistant chat failed.";
    handlers.onError?.(detail);
    throw new Error(detail);
  }
}
async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);

  if (!response.ok) {
    throw new ApiRequestError(response.status, await errorMessageFromResponse(response));
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

export function fetchMdDemoDefaults(): Promise<MdDemoDefaultsResponse> {
  return getJSON("/md-demo/defaults");
}

export function runMdDemo(payload: MdDemoRunRequest): Promise<MdDemoRunResponse> {
  return postJSON("/md-demo/run", payload);
}

export function calculateMdDemoAtomDistance(payload: MdDemoAtomDistanceRequest): Promise<MdDemoAtomDistanceResponse> {
  return postJSON("/md-demo/atom-distance", payload);
}

function normalizeMonomerMdJob(job: MonomerMdJobResponse): MonomerMdJobResponse {
  return {
    ...job,
    smiles: job.smiles ?? job.input_smiles ?? job.canonical_smiles,
    progress: job.progress ?? job.progress_percent ?? null,
    message: job.message ?? job.progress_message ?? null,
    error: job.error ?? job.error_message ?? null
  };
}
export function fetchMonomerMdStatus(signal?: AbortSignal): Promise<MonomerMdServiceStatusResponse> {
  return getJSON("/monomer-md/status", { signal });
}

export function fetchMonomerMdProtocols(signal?: AbortSignal): Promise<MonomerMdProtocolCatalogResponse> {
  return getJSON("/monomer-md/protocols", { signal });
}

export function createMonomerMdJob(payload: MonomerMdJobCreateRequest): Promise<MonomerMdJobCreateResponse> {
  return postJSON("/monomer-md/jobs", payload);
}

export async function fetchMonomerMdJob(jobId: string): Promise<MonomerMdJobResponse> {
  const job = await getJSON<MonomerMdJobResponse>(`/monomer-md/jobs/${encodeURIComponent(jobId)}`);
  return normalizeMonomerMdJob(job);
}

export async function deleteMonomerMdArtifacts(jobId: string): Promise<MonomerMdJobResponse> {
  const response = await fetch(`${API_BASE_URL}/monomer-md/jobs/${encodeURIComponent(jobId)}/artifacts`, {
    method: "DELETE"
  });

  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }

  return normalizeMonomerMdJob((await response.json()) as MonomerMdJobResponse);
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

export function fetchConditionalGenerationTgStatus(): Promise<ConditionalGenerationTgStatusResponse> {
  return getJSON("/conditional-generation/tg/status");
}

export async function fetchPolytaoStatus(): Promise<PolytaoStatusResponse> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), 10_000);
  try {
    return await getJSON("/conditional-generation/polytao/status", {
      cache: "no-store",
      signal: controller.signal
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error("PolyTAO runtime status request timed out after 10 seconds.");
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

export function calculatePolytaoDescriptors(
  payload: PolytaoDescriptorRequest
): Promise<PolytaoDescriptorResponse> {
  return postJSON("/conditional-generation/polytao/descriptors", payload);
}

export function createPolytaoJob(payload: PolytaoGenerationRequest): Promise<PolytaoJobCreateResponse> {
  return postJSON("/conditional-generation/polytao/jobs", payload);
}

export function fetchPolytaoJob(jobId: string): Promise<PolytaoJobStatusResponse> {
  return getJSON(`/conditional-generation/polytao/jobs/${encodeURIComponent(jobId)}`);
}

export function fetchStructure3D(
  smiles: string
): Promise<{ molblock: string; capped_smiles: string; format: "mol" }> {
  return postJSON("/structure/3d", { smiles });
}

export function standardizeSmiles(payload: SmilesStandardizeRequest): Promise<SmilesStandardizeResponse> {
  return postJSON("/structure/standardize-smiles", payload);
}

export function recognizeStructureImage(file: File, signal?: AbortSignal): Promise<StructureImageRecognitionResponse> {
  const body = new FormData();
  body.append("image", file);
  return postForm("/structure/recognize-image", body, signal);
}

export function predictMonomerPrecursors(
  payload: MonomerRetrosynthesisRequest
): Promise<MonomerRetrosynthesisResponse> {
  return postJSON("/monomer-retrosynthesis", payload);
}

export function fetchMonomerPolymerizationStatus(): Promise<MonomerPolymerizationStatusResponse> {
  return getJSON("/monomer-polymerization/status");
}

export function runMonomerPolymerization(
  payload: MonomerPolymerizationRequest
): Promise<MonomerPolymerizationResponse> {
  return postJSON("/monomer-polymerization", payload);
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

export function fetchPropertyFilterOptions(): Promise<PropertyFilterOptionsResponse> {
  return getJSON("/database-browser/property-filter/options");
}

export function searchPropertyFilterRecords(
  payload: PropertyFilterSearchRequest
): Promise<PropertyFilterSearchResponse> {
  return postJSON("/database-browser/property-filter/search", payload);
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

export function fetchDatabaseDatasetSummary(): Promise<DatasetSummaryResponse> {
  return getJSON("/database-browser/datasets/summary");
}

export function fetchDatabaseAnalytics(options?: { refresh?: boolean }): Promise<DatabaseAnalyticsResponse> {
  return getJSON(`/database-browser/datasets/analytics${options?.refresh ? "?refresh=true" : ""}`);
}

export function browseFormulationRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}): Promise<FormulationBrowseResponse> {
  return getJSON(`/database-browser/formulation${buildQueryString(params)}`);
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
