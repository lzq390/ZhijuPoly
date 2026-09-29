import { expect } from "vitest";
/** Expected transport envelope; domain tests retain exact URL/body/header checks. */
export function authenticatedRequest(init: RequestInit) {
  const headers = new Headers(init.headers);
  headers.set("X-Session-Context", "test-session");
  if (!["GET", "HEAD", "OPTIONS"].includes((init.method ?? "GET").toUpperCase())) headers.set("X-CSRF-Token", "test-csrf");
  return { ...init, credentials: "same-origin", cache: "no-store", headers, signal: expect.any(AbortSignal) };
}
