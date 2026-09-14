import { API_BASE_URL, ApiRequestError } from "./api";
import type { MonomerPolymerizationTargetClass } from "../types";
import type { BatchImport, BatchImportPreview, BatchJob, BatchMapping, BatchResults } from "../types/polymerizationBatch";

export function batchUrl(path: string) {
  return `${API_BASE_URL}/monomer-polymerization/batch${path}`;
}
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(batchUrl(path), { cache: "no-store", ...init });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiRequestError(response.status, data?.message ?? data?.detail ?? `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}
export function uploadBatchTables(a: File, b: File, signal?: AbortSignal) {
  const body = new FormData();
  body.append("file_a", a);
  body.append("file_b", b);
  return request<BatchImport>("/imports", { method: "POST", body, signal });
}
export function previewBatchTables(id: string, mappings: Record<"a" | "b", BatchMapping>, signal?: AbortSignal) {
  return request<BatchImportPreview>(`/imports/${encodeURIComponent(id)}/preview`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(mappings), signal
  });
}
export function submitBatchJob(importId: string, revision: string, target: MonomerPolymerizationTargetClass, idempotencyKey: string, signal?: AbortSignal) {
  return request<BatchJob>("/jobs", { method: "POST", signal,
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ import_id: importId, preview_revision: revision, target_class: target }) });
}
export function fetchBatchJob(id: string, signal?: AbortSignal) {
  return request<BatchJob>(`/jobs/${encodeURIComponent(id)}`, { signal });
}
export function fetchBatchResults(id: string, offset = 0, signal?: AbortSignal) {
  return request<BatchResults>(`/jobs/${encodeURIComponent(id)}/results?offset=${offset}&limit=50`, { signal });
}
export function cancelBatchJob(id: string, signal?: AbortSignal) {
  return request<BatchJob>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", signal });
}

export class BatchArtifactError extends Error {
  constructor(readonly status: number, message: string, readonly code?: string) {
    super(message);
    this.name = "BatchArtifactError";
  }
}

export async function downloadBatchArtifact(id: string, name: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(batchUrl(`/jobs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(name)}`), { cache: "no-store", signal });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const message = typeof data?.message === "string" ? data.message : typeof data?.detail === "string" ? data.detail
      : response.status === 410 ? "结果文件已过期或不可用。" : `下载失败 (${response.status})，请稍后重试。`;
    throw new BatchArtifactError(response.status, message, typeof data?.code === "string" ? data.code : undefined);
  }
  return response.blob();
}
