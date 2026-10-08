import { privateFetch } from '../auth/session';
import type { StructureDocument, StructureLibraryStatus, StructureRecord, StructureRecordSummary } from './types';
const root = '/api/v1/structure-library';
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await privateFetch(root + path, { ...init, headers: { 'Content-Type': 'application/json', ...init.headers } });
  if (response.status === 204) return undefined as T;
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || '结构库操作失败，请重试。');
  return body as T;
}
export const structureLibraryApi = {
  status: (signal?: AbortSignal) => request<StructureLibraryStatus>('/status', { signal }),
  list: (q: string, cursor?: string | null, signal?: AbortSignal) => request<{ items: StructureRecordSummary[]; next_cursor: string | null }>(
    '/records?' + new URLSearchParams({ q, ...(cursor ? { cursor } : {}) }), { signal }),
  detail: (id: string, signal?: AbortSignal) => request<StructureRecord>('/records/' + encodeURIComponent(id), { signal }),
  save: (document: StructureDocument, name: string, requestId: string, signal?: AbortSignal) => request<StructureRecord>('/records', {
    method: 'POST', signal, body: JSON.stringify({ name, smiles: document.smiles, ket: JSON.parse(document.ket),
      snapshot_schema_version: document.snapshot_schema_version, editor_version: document.editor_version, client_request_id: requestId })
  }),
  rename: (record: StructureRecordSummary, name: string, signal?: AbortSignal) => request<StructureRecordSummary>('/records/' + record.id, {
    method: 'PATCH', signal, body: JSON.stringify({ name, metadata_revision: record.metadata_revision })
  }),
  remove: (id: string, signal?: AbortSignal) => request<void>('/records/' + id, { method: 'DELETE', signal })
};
