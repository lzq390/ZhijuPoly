import type { SmilesQueryRequest, SmilesQueryResponse } from "../types";

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

export function querySmiles(payload: SmilesQueryRequest): Promise<SmilesQueryResponse> {
  return postJSON("/query/smiles", payload);
}

export function fetchStructure3D(
  smiles: string
): Promise<{ molblock: string; capped_smiles: string; format: "mol" }> {
  return postJSON("/structure/3d", { smiles });
}
