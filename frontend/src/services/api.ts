import type { SmilesQueryRequest, SmilesQueryResponse } from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

export async function querySmiles(
  payload: SmilesQueryRequest
): Promise<SmilesQueryResponse> {
  const response = await fetch(`${API_BASE_URL}/query/smiles`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const message =
      typeof data?.detail === "string" ? data.detail : `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  return (await response.json()) as SmilesQueryResponse;
}
