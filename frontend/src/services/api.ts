import type {
  DftMoleculeDetail,
  DftPcaSampleResponse,
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  OnlineKnowledgeExportResponse,
  OnlineKnowledgeHistoryResponse,
  OnlineKnowledgeSearchRequest,
  OnlineKnowledgeSearchResponse,
  PredictRequest,
  PredictResponse,
  ReverseDesignKnowledgeRequest,
  ReverseDesignKnowledgeResponse,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse,
  SmilesQueryRequest,
  SmilesQueryResponse
} from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const message =
      typeof data?.detail === "string" ? data.detail : `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  return (await response.json()) as T;
}

async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);

  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const message =
      typeof data?.detail === "string" ? data.detail : `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  return (await response.json()) as T;
}

export function querySmiles(payload: SmilesQueryRequest): Promise<SmilesQueryResponse> {
  return postJSON("/query/smiles", payload);
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
    const data = await response.json().catch(() => null);
    const message =
      typeof data?.detail === "string" ? data.detail : `Request failed with status ${response.status}`;
    throw new Error(message);
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

export function fetchReverseDesignKnowledge(
  payload: ReverseDesignKnowledgeRequest
): Promise<ReverseDesignKnowledgeResponse> {
  return postJSON("/reverse-design/knowledge", payload);
}

export function fetchStructure3D(
  smiles: string
): Promise<{ molblock: string; capped_smiles: string; format: "mol" }> {
  return postJSON("/structure/3d", { smiles });
}

export function fetchDftPcaSample(limit = 200): Promise<DftPcaSampleResponse> {
  return getJSON(`/dft/pca-sample?limit=${limit}`);
}

export function fetchDftMolecule(molId: string): Promise<DftMoleculeDetail> {
  return getJSON(`/dft/molecule/${encodeURIComponent(molId)}`);
}
