import type {
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
  DevGpuSessionStatusResponse,
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
  MonomerDftArtifactDeleteResponse,
  MonomerDftCapabilitiesResponse,
  MonomerDftJobCreateRequest,
  MonomerDftJobCreateResponse,
  MonomerDftJobListQuery,
  MonomerDftJobListResponse,
  MonomerDftJobResponse,
  MonomerDftModelName,
  MonomerDftServiceStatusResponse,
  MonomerMdJobCreateRequest,
  MonomerMdJobCreateResponse,
  MonomerMdJobListQuery,
  MonomerMdJobPageResponse,
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
  PropertyFilterHistogramResponse,
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

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

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

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal
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

export function fetchDevGpuSessionStatus(signal?: AbortSignal): Promise<DevGpuSessionStatusResponse> {
  return getJSON("/dev-gpu-session/status", { cache: "no-store", signal });
}

export function recoverDevGpuSession(signal?: AbortSignal): Promise<DevGpuSessionStatusResponse> {
  return postJSON("/dev-gpu-session/recover", {}, signal);
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

export class MonomerDftApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly retryable: boolean;
  readonly retryAfterSeconds: number | null;
  readonly details: unknown;

  constructor(options: {
    message: string;
    status: number;
    code?: string | null;
    retryable?: boolean;
    retryAfterSeconds?: number | null;
    details?: unknown;
  }) {
    super(options.message);
    this.name = "MonomerDftApiError";
    this.status = options.status;
    this.code = options.code ?? null;
    this.retryable = options.retryable ?? (options.status === 429 || options.status >= 500);
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
    this.details = options.details;
  }
}

export function parseMonomerDftRetryAfterSeconds(
  value: string | null,
  nowMs = Date.now()
): number | null {
  if (value == null) return null;
  const normalized = value.trim();
  if (!normalized) return null;

  let seconds: number;
  if (/^\d+$/.test(normalized)) {
    seconds = Number(normalized);
  } else {
    const retryAtMs = Date.parse(normalized);
    if (!Number.isFinite(retryAtMs)) return null;
    seconds = Math.ceil((retryAtMs - nowMs) / 1_000);
  }
  if (!Number.isFinite(seconds)) return null;
  return Math.min(60, Math.max(1, Math.ceil(seconds)));
}

async function monomerDftRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as {
      detail?: string | { code?: string; message?: string; retryable?: boolean; details?: unknown } | unknown[];
      code?: string;
      message?: string;
      retryable?: boolean;
      details?: unknown;
    } | null;
    const structuredDetail = payload?.detail && typeof payload.detail === "object" && !Array.isArray(payload.detail)
      ? payload.detail
      : null;
    const message =
      (typeof payload?.detail === "string" ? payload.detail : null) ??
      structuredDetail?.message ??
      payload?.message ??
      (Array.isArray(payload?.detail) ? "单体 DFT 请求参数校验失败。" : `单体 DFT 请求失败（${response.status}）。`);
    const retryAfterSeconds = parseMonomerDftRetryAfterSeconds(response.headers.get("Retry-After"));
    throw new MonomerDftApiError({
      message,
      status: response.status,
      code: structuredDetail?.code ?? payload?.code ?? null,
      retryable: structuredDetail?.retryable ?? payload?.retryable,
      retryAfterSeconds,
      details: structuredDetail?.details ?? payload?.details ?? payload?.detail
    });
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return await response.json() as T;
}

export function fetchMonomerDftStatus(signal?: AbortSignal): Promise<MonomerDftServiceStatusResponse> {
  return monomerDftRequest("/monomer-dft/status", { signal });
}

export function fetchMonomerDftCapabilities(signal?: AbortSignal): Promise<MonomerDftCapabilitiesResponse> {
  return monomerDftRequest<unknown>("/monomer-dft/capabilities", { signal }).then(normalizeMonomerDftCapabilities);
}

const MONOMER_DFT_MODEL_FALLBACKS: Record<MonomerDftModelName, {
  label: string;
  description: string;
  elements: string[];
  supportsSpin: boolean;
  deprecated?: boolean;
}> = {
  aimnet2: { label: "AIMNet2", description: "通用有机分子默认模型。", elements: ["H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "As", "Se", "Br", "I"], supportsSpin: false },
  "aimnet2-2025": { label: "AIMNet2 2025", description: "新一代 B97-3c 通用模型。", elements: ["H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "As", "Se", "Br", "I"], supportsSpin: false },
  "aimnet2-b973c": { label: "AIMNet2 B97-3c", description: "旧版 B97-3c 模型。", elements: ["H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "As", "Se", "Br", "I"], supportsSpin: false, deprecated: true },
  "aimnet2-nse": { label: "AIMNet2 NSE", description: "开放壳层、自由基和键解离模型。", elements: ["H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "As", "Se", "Br", "I"], supportsSpin: true },
  "aimnet2-pd": { label: "AIMNet2 Pd", description: "含钯催化体系模型。", elements: ["H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "Se", "Br", "Pd", "I"], supportsSpin: false },
  "aimnet2-rxn": { label: "AIMNet2 RXN", description: "H/C/N/O 反应化学模型。", elements: ["H", "C", "N", "O"], supportsSpin: false }
};

function isMonomerDftModelName(value: string): value is MonomerDftModelName {
  return Object.prototype.hasOwnProperty.call(MONOMER_DFT_MODEL_FALLBACKS, value);
}

function normalizeMonomerDftCapabilities(value: unknown): MonomerDftCapabilitiesResponse {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const calculationTypes: MonomerDftCapabilitiesResponse["calculation_types"] = Array.isArray(raw.calculation_types)
    ? raw.calculation_types.filter((item): item is "single_point" | "optimization" => item === "single_point" || item === "optimization")
    : ["single_point", "optimization"];
  const properties: MonomerDftCapabilitiesResponse["properties"] = Array.isArray(raw.properties)
    ? raw.properties.filter((item): item is MonomerDftCapabilitiesResponse["properties"][number] => ["energy", "charges", "forces", "hessian", "frequencies"].includes(String(item)))
    : ["energy", "charges", "forces", "hessian", "frequencies"];
  const rawModels = Array.isArray(raw.models) ? raw.models : [];
  const models: MonomerDftCapabilitiesResponse["models"] = rawModels.flatMap((item) => {
    const model = item && typeof item === "object" ? item as Record<string, unknown> : null;
    const candidateId = typeof item === "string" ? item : typeof model?.id === "string" ? model.id : "";
    if (!isMonomerDftModelName(candidateId)) return [];
    const id = candidateId;
    const fallback = MONOMER_DFT_MODEL_FALLBACKS[id];
    const supportedCalculationTypes: MonomerDftCapabilitiesResponse["calculation_types"] = Array.isArray(model?.supported_calculation_types)
      ? model.supported_calculation_types.filter((type): type is "single_point" | "optimization" => type === "single_point" || type === "optimization")
      : calculationTypes;
    const supportedProperties: MonomerDftCapabilitiesResponse["properties"] = Array.isArray(model?.supported_properties)
      ? model.supported_properties.filter((property): property is MonomerDftCapabilitiesResponse["properties"][number] => properties.includes(property as MonomerDftCapabilitiesResponse["properties"][number]))
      : properties;
    return [{
      id,
      label: typeof model?.label === "string" ? model.label : fallback.label,
      description: typeof model?.description === "string" ? model.description : fallback.description,
      available: typeof model?.available === "boolean" ? model.available : Boolean(raw.available ?? true),
      is_default: typeof model?.is_default === "boolean" ? model.is_default : id === (raw.default_model ?? "aimnet2"),
      deprecated: typeof model?.deprecated === "boolean" ? model.deprecated : Boolean(fallback.deprecated),
      deprecation_message: typeof model?.deprecation_message === "string" ? model.deprecation_message : fallback.deprecated ? "仅为兼容保留，优先选择 AIMNet2 2025。" : null,
      supported_calculation_types: supportedCalculationTypes,
      supported_properties: supportedProperties,
      supported_elements: Array.isArray(model?.supported_elements) ? model.supported_elements.filter((element): element is string => typeof element === "string") : fallback.elements,
      supports_spin: typeof model?.supports_spin === "boolean" ? model.supports_spin : fallback.supportsSpin,
      charge_min: typeof model?.charge_min === "number" ? model.charge_min : -5,
      charge_max: typeof model?.charge_max === "number" ? model.charge_max : 5
    }];
  });
  const rawDefaults = raw.defaults && typeof raw.defaults === "object" ? raw.defaults as Record<string, unknown> : {};
  const rawConformer = rawDefaults.conformer && typeof rawDefaults.conformer === "object" ? rawDefaults.conformer as Record<string, unknown> : {};
  const rawSinglePoint = rawDefaults.single_point && typeof rawDefaults.single_point === "object" ? rawDefaults.single_point as Record<string, unknown> : {};
  const rawOptimization = rawDefaults.optimization && typeof rawDefaults.optimization === "object" ? rawDefaults.optimization as Record<string, unknown> : {};
  const limits = raw.limits && typeof raw.limits === "object" ? raw.limits as Record<string, number> : {};
  return {
    enabled: Boolean(raw.enabled ?? true),
    available: Boolean(raw.available ?? false),
    schema_ready: raw.schema_ready === true,
    calculation_types: calculationTypes,
    properties,
    default_model: typeof raw.default_model === "string" && isMonomerDftModelName(raw.default_model) ? raw.default_model : models.find((model) => model.is_default)?.id ?? "aimnet2",
    models,
    defaults: {
      conformer: {
        seed: typeof rawConformer.seed === "number" ? rawConformer.seed : 1,
        max_iterations: typeof rawConformer.max_iterations === "number" ? rawConformer.max_iterations : 500
      },
      single_point: {
        properties: Array.isArray(rawSinglePoint.properties) ? rawSinglePoint.properties as MonomerDftCapabilitiesResponse["properties"] : ["energy", "charges", "forces"]
      },
      optimization: {
        fmax_eV_per_A: typeof rawOptimization.fmax_eV_per_A === "number" ? rawOptimization.fmax_eV_per_A : 0.01,
        max_steps: typeof rawOptimization.max_steps === "number" ? rawOptimization.max_steps : 50,
        post_optimization_properties: Array.isArray(rawOptimization.post_optimization_properties)
          ? rawOptimization.post_optimization_properties.filter((item): item is "hessian" | "frequencies" => item === "hessian" || item === "frequencies")
          : []
      }
    },
    limits: {
      ...limits,
      max_optimization_steps: limits.max_optimization_steps ?? 50,
      min_optimization_steps: limits.min_optimization_steps ?? 10,
      max_concurrent_jobs: limits.max_concurrent_jobs ?? 1,
      max_queued_jobs: limits.max_queued_jobs ?? 8,
      max_active_jobs: limits.max_active_jobs ?? (limits.max_concurrent_jobs ?? 1) + (limits.max_queued_jobs ?? 8)
    },
    worker: raw.worker && typeof raw.worker === "object" ? raw.worker as Record<string, unknown> : {},
    message: typeof raw.message === "string" ? raw.message : null
  };
}

export function createMonomerDftJob(
  payload: MonomerDftJobCreateRequest,
  idempotencyKey: string,
  signal?: AbortSignal
): Promise<MonomerDftJobCreateResponse> {
  return monomerDftRequest("/monomer-dft/jobs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey
    },
    body: JSON.stringify(payload),
    signal
  });
}

export function fetchMonomerDftJob(jobId: string, signal?: AbortSignal): Promise<MonomerDftJobResponse> {
  return monomerDftRequest(`/monomer-dft/jobs/${encodeURIComponent(jobId)}`, { signal });
}

export function fetchMonomerDftJobs(
  query: MonomerDftJobListQuery,
  signal?: AbortSignal
): Promise<MonomerDftJobListResponse> {
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.page_size)
  });
  if (query.status) params.set("status", query.status);
  if (query.calculation_type) params.set("calculation_type", query.calculation_type);
  return monomerDftRequest(`/monomer-dft/jobs?${params.toString()}`, { signal });
}

export function cancelMonomerDftJob(jobId: string, signal?: AbortSignal): Promise<MonomerDftJobResponse> {
  return monomerDftRequest(`/monomer-dft/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    signal
  });
}

export function deleteMonomerDftArtifacts(
  jobId: string,
  signal?: AbortSignal
): Promise<MonomerDftArtifactDeleteResponse> {
  return monomerDftRequest(`/monomer-dft/jobs/${encodeURIComponent(jobId)}/artifacts`, {
    method: "DELETE",
    signal
  });
}

export function deleteMonomerDftJob(
  jobId: string,
  signal?: AbortSignal
): Promise<void> {
  return monomerDftRequest(`/monomer-dft/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
    signal
  });
}

export async function deleteMonomerDftArtifactsAndReloadJob(
  jobId: string,
  signal?: AbortSignal
): Promise<MonomerDftJobResponse> {
  await deleteMonomerDftArtifacts(jobId, signal);
  return fetchMonomerDftJob(jobId, signal);
}

export function getMonomerDftArtifactUrl(jobId: string, artifactId: string): string {
  return `${API_BASE_URL}/monomer-dft/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}`;
}

export function getMonomerDftBundleUrl(jobId: string): string {
  return `${API_BASE_URL}/monomer-dft/jobs/${encodeURIComponent(jobId)}/bundle`;
}

export function fetchMonomerDftArtifactJson<T>(jobId: string, artifactId: string, signal?: AbortSignal): Promise<T> {
  return monomerDftRequest(`/monomer-dft/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}`, { signal });
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

export function createMonomerMdJob(
  payload: MonomerMdJobCreateRequest,
  signal?: AbortSignal
): Promise<MonomerMdJobCreateResponse> {
  return postJSON("/monomer-md/jobs", payload, signal);
}

export async function fetchMonomerMdJob(jobId: string, signal?: AbortSignal): Promise<MonomerMdJobResponse> {
  const job = await getJSON<MonomerMdJobResponse>(`/monomer-md/jobs/${encodeURIComponent(jobId)}`, { signal });
  return normalizeMonomerMdJob(job);
}

export async function fetchMonomerMdJobs(
  query: MonomerMdJobListQuery,
  signal?: AbortSignal
): Promise<MonomerMdJobPageResponse> {
  const params = new URLSearchParams();
  if (query.run_mode) params.set("run_mode", query.run_mode);
  if (query.active_only != null) params.set("active_only", String(query.active_only));
  if (query.protocol) params.set("protocol", query.protocol);
  if (query.status) params.set("status", query.status);
  if (query.page != null) params.set("page", String(query.page));
  if (query.page_size != null) params.set("page_size", String(query.page_size));
  const page = await getJSON<MonomerMdJobPageResponse>(`/monomer-md/jobs?${params.toString()}`, { signal });
  return { ...page, items: page.items.map(normalizeMonomerMdJob) };
}

export async function cancelMonomerMdJob(
  jobId: string,
  signal?: AbortSignal
): Promise<MonomerMdJobResponse> {
  const job = await postJSON<MonomerMdJobResponse>(
    `/monomer-md/jobs/${encodeURIComponent(jobId)}/cancel`,
    {},
    signal
  );
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

export async function deleteMonomerMdJob(
  jobId: string,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/monomer-md/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE", signal }
  );
  if (!response.ok) {
    throw new Error(await errorMessageFromResponse(response));
  }
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
  payload: OnlineKnowledgeSearchRequest,
  signal?: AbortSignal
): Promise<OnlineKnowledgeJobCreateResponse> {
  return postJSON("/online-knowledge/jobs", payload, signal);
}

export function fetchOnlineKnowledgeDefaultConfig(): Promise<OnlineKnowledgeDefaultConfigResponse> {
  return getJSON("/online-knowledge/default-config");
}

export function fetchOnlineKnowledgeJob(jobId: string, signal?: AbortSignal): Promise<OnlineKnowledgeJobResponse> {
  return getJSON(`/online-knowledge/jobs/${encodeURIComponent(jobId)}`, { signal });
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
  payload: ReverseDesignTgRequest,
  signal?: AbortSignal
): Promise<ReverseDesignTgJobCreateResponse> {
  return postJSON("/reverse-design/tg/jobs", payload, signal);
}

export function fetchReverseDesignTgJob(jobId: string, signal?: AbortSignal): Promise<ReverseDesignTgJobStatusResponse> {
  return getJSON(`/reverse-design/tg/jobs/${encodeURIComponent(jobId)}`, { signal });
}

export function createConditionalGenerationTgJob(
  payload: ConditionalGenerationTgRequest,
  signal?: AbortSignal
): Promise<ConditionalGenerationJobCreateResponse> {
  return postJSON("/conditional-generation/tg/jobs", payload, signal);
}

export function fetchConditionalGenerationTgJob(
  jobId: string,
  signal?: AbortSignal
): Promise<ConditionalGenerationJobStatusResponse> {
  return getJSON(`/conditional-generation/tg/jobs/${encodeURIComponent(jobId)}`, { signal });
}

export function fetchConditionalGenerationTgStatus(signal?: AbortSignal): Promise<ConditionalGenerationTgStatusResponse> {
  return getJSON("/conditional-generation/tg/status", { signal });
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

export function fetchDftPcaSample(limit = 200, signal?: AbortSignal): Promise<DftPcaSampleResponse> {
  return getJSON(`/dft/pca-sample?limit=${limit}`, { signal });
}

export function fetchDftMolecule(molId: string, signal?: AbortSignal): Promise<DftMoleculeDetail> {
  return getJSON(`/dft/molecule/${encodeURIComponent(molId)}`, { signal });
}

export function browseStructurePropertyRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}, signal?: AbortSignal): Promise<StructurePropertyBrowseResponse> {
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
  return getJSON(`/database-browser/structure-property${suffix}`, { signal });
}

export type PropertyFilterOptionsFetchResult =
  | { status: "success"; data: PropertyFilterOptionsResponse; etag: string | null }
  | { status: "not-modified"; data: null; etag: string | null };

export async function fetchPropertyFilterOptions(options: {
  etag?: string | null;
  signal?: AbortSignal;
} = {}): Promise<PropertyFilterOptionsFetchResult> {
  const headers = new Headers();
  if (options.etag) headers.set("If-None-Match", options.etag);
  const response = await fetch(`${API_BASE_URL}/database-browser/property-filter/options`, {
    cache: "no-cache",
    headers,
    signal: options.signal
  });
  const etag = response.headers.get("ETag") ?? options.etag ?? null;
  if (response.status === 304) {
    return { status: "not-modified", data: null, etag };
  }
  if (!response.ok) {
    throw new ApiRequestError(response.status, await errorMessageFromResponse(response));
  }
  return {
    status: "success",
    data: (await response.json()) as PropertyFilterOptionsResponse,
    etag
  };
}

export type PropertyFilterHistogramFetchResult =
  | { status: "success"; data: PropertyFilterHistogramResponse; etag: string | null }
  | { status: "not-modified"; data: null; etag: string | null };

export async function fetchPropertyFilterHistogram(
  optionKey: string,
  options: { etag?: string | null; signal?: AbortSignal } = {}
): Promise<PropertyFilterHistogramFetchResult> {
  const headers = new Headers();
  if (options.etag) headers.set("If-None-Match", options.etag);
  const searchParams = new URLSearchParams({ option_key: optionKey });
  const response = await fetch(
    `${API_BASE_URL}/database-browser/property-filter/histogram?${searchParams.toString()}`,
    { cache: "no-cache", headers, signal: options.signal }
  );
  const etag = response.headers.get("ETag") ?? options.etag ?? null;
  if (response.status === 304) {
    return { status: "not-modified", data: null, etag };
  }
  if (!response.ok) {
    throw new ApiRequestError(response.status, await errorMessageFromResponse(response));
  }
  return {
    status: "success",
    data: (await response.json()) as PropertyFilterHistogramResponse,
    etag
  };
}

export function searchPropertyFilterRecords(
  payload: PropertyFilterSearchRequest,
  signal?: AbortSignal
): Promise<PropertyFilterSearchResponse> {
  return postJSON("/database-browser/property-filter/search", payload, signal);
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
}, signal?: AbortSignal): Promise<DftMoleculeBrowseResponse> {
  return getJSON(`/database-browser/dft/molecules${buildQueryString(params)}`, { signal });
}

export function browseDftEnergySteps(params: {
  q?: string;
  mol_id?: string;
  page?: number;
  page_size?: number;
}, signal?: AbortSignal): Promise<DftEnergyStepBrowseResponse> {
  return getJSON(`/database-browser/dft/steps${buildQueryString(params)}`, { signal });
}

export function fetchDatabaseDatasetSummary(signal?: AbortSignal): Promise<DatasetSummaryResponse> {
  return getJSON("/database-browser/datasets/summary", { signal });
}

export function fetchDatabaseAnalytics(options?: { refresh?: boolean; signal?: AbortSignal }): Promise<DatabaseAnalyticsResponse> {
  return getJSON(`/database-browser/datasets/analytics${options?.refresh ? "?refresh=true" : ""}`, { signal: options?.signal });
}

export function browseFormulationRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}, signal?: AbortSignal): Promise<FormulationBrowseResponse> {
  return getJSON(`/database-browser/formulation${buildQueryString(params)}`, { signal });
}

export function browseExperimentalProcessRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}, signal?: AbortSignal): Promise<ExperimentalProcessBrowseResponse> {
  return getJSON(`/database-browser/experimental-process${buildQueryString(params)}`, { signal });
}

export function browseExperimentalPropertyRecords(params: {
  q?: string;
  page?: number;
  page_size?: number;
}, signal?: AbortSignal): Promise<ExperimentalPropertyBrowseResponse> {
  return getJSON(`/database-browser/experimental-property${buildQueryString(params)}`, { signal });
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
