import { apiPath, attachUsageResponse, confirmUsageResponse, installUsageIdentity, isUsageSubmission, observeUsageEnabled, prepareUsageSubmission, usageFailure, UsageOperationError } from "./usageAdmission";
/** Browser identity fence shared by JSON, downloads, SSE and local async writes. */
export type AuthSession = {
  authenticated: boolean;
  user: { id: string; username: string; must_change_password: boolean } | null;
  session_id: string | null;
  csrf_token: string | null;
  capabilities: Record<string, boolean>;
  account_type?: "free" | "member";
};
export const GUEST_SESSION: AuthSession = {
  authenticated: false, user: null, session_id: null, csrf_token: null, capabilities: {}
};
let session = GUEST_SESSION;
let epoch = 0;
let ready = false;
const requests = new Set<AbortController>();
const retirementListeners = new Set<() => void>();
const invalidationListeners = new Set<() => void>();
export const getSession = () => session;
export const getSessionEpoch = () => epoch;
export const sessionIsReady = () => ready;
export const staleSessionError = () => new DOMException("登录状态已改变，请重新操作。", "AbortError");
export function assertSessionEpoch(expected: number) {
  if (expected !== epoch) throw staleSessionError();
}
export function onSessionRetired(listener: () => void) {
  retirementListeners.add(listener);
  return () => { retirementListeners.delete(listener); };
}
export function onSessionInvalidated(listener: () => void) {
  invalidationListeners.add(listener);
  return () => { invalidationListeners.delete(listener); };
}
export function retireSession() {
  ready = false;
  installUsageIdentity("");
  ++epoch;
  for (const request of requests) request.abort();
  requests.clear();
  for (const listener of retirementListeners) listener();
}
export function installSession(next: AuthSession) {
  session = next;
  installUsageIdentity(next.authenticated && next.user && next.session_id ? `${next.user.id}:${next.session_id}` : "");
  ready = true;
}

/** Auth endpoints use the raw transport so a guest can establish a session. */
export type PrivateRequestInit = RequestInit & { usageIntent?: string };
export async function privateFetch(input: RequestInfo | URL, init: PrivateRequestInit = {}): Promise<Response> {
  if (!ready) throw staleSessionError();
  if (!session.authenticated || !session.session_id) throw new Error("请登录账号。");
  const expected = epoch;
  const controller = new AbortController();
  const sourceRequest = typeof Request !== "undefined" && input instanceof Request ? input : null;
  const url = sourceRequest?.url ?? String(input);
  const path = apiPath(url);
  if (session.account_type === "free" && path && !path.startsWith("/usage/") && !path.startsWith("/auth/") && !/^\/monomer-polymerization\/batch\/templates\/[ab]\.(csv|xlsx)$/.test(path)) throw new UsageOperationError(403, "membership_required");
  const sourceSignal = init.signal ?? sourceRequest?.signal;
  const abort = () => controller.abort();
  sourceSignal?.addEventListener("abort", abort, { once: true });
  if (sourceSignal?.aborted) controller.abort();
  requests.add(controller);
  const cleanup = () => {
    requests.delete(controller);
    sourceSignal?.removeEventListener("abort", abort);
  };
  const check = () => {
    assertSessionEpoch(expected);
    if (controller.signal.aborted) throw staleSessionError();
  };
  const headers = new Headers(init.headers ?? sourceRequest?.headers);
  headers.set("X-Session-Context", session.session_id);
  const method = (init.method ?? sourceRequest?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers.set("X-CSRF-Token", session.csrf_token ?? "");
  try {
    check();
    let body = init.body;
    if (body === undefined && sourceRequest && isUsageSubmission(url, method)) body = sourceRequest.headers.get("content-type")?.includes("multipart/form-data") ? await sourceRequest.clone().formData() : await sourceRequest.clone().text();
    const ticket = isUsageSubmission(url, method) ? await prepareUsageSubmission({ url, method, body, intent: init.usageIntent, headers, check, fetcher: target => privateFetch(target, { signal: controller.signal }) }) : undefined;
    check();
    const { usageIntent: _usageIntent, ...transportInit } = init;
    const response = await fetch(input, { ...transportInit, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    check();
    attachUsageResponse(response, ticket);
    let invalidIdentity = response.status === 401;
    let operationFailure: Error | undefined;
    if (response.status === 409 || response.status === 403) {
      const failure = await response.clone().json().catch(() => null);
      check();
      invalidIdentity = ["session_context_mismatch", "password_change_required"].includes(failure?.code);
      operationFailure = usageFailure(response, failure, ticket);
    }
    if (invalidIdentity) {
      // Check the originating epoch before dispatch: a late A/401 must not log B out.
      retireSession();
      for (const listener of invalidationListeners) listener();
      throw staleSessionError();
    }
    if (operationFailure) throw operationFailure;
    if ([204, 205, 304].includes(response.status) || method === "HEAD") cleanup();
    const wrapped = new Proxy(response, {
      get(target, key) {
        if (["json", "text", "blob", "arrayBuffer", "formData"].includes(String(key))) {
          return async () => {
            try {
              check();
              const result = await (target[key as keyof Response] as () => Promise<unknown>).call(target);
              check();
              if (key === "json" && response.ok) {
                if (path === "/usage/entitlements") observeUsageEnabled(result);
                const acceptedJob = result && typeof result === "object" && "job_id" in result && typeof result.job_id === "string" && result.job_id.length > 0;
                if (!response.headers.get("content-type")?.includes("text/event-stream") && (!path?.endsWith("/jobs") || acceptedJob || method !== "POST")) confirmUsageResponse(response);
              }
              return result;
            } finally { cleanup(); }
          };
        }
        if (key === "body" && target.body) {
          return new Proxy(target.body, {
            get(body, bodyKey) {
              if (bodyKey === "getReader") return () => {
                const reader = body.getReader();
                const cancel = () => { void reader.cancel().catch(() => {}); };
                controller.signal.addEventListener("abort", cancel, { once: true });
                return {
                  async read() {
                    check();
                    const result = await reader.read();
                    check();
                    if (result.done) cleanup();
                    return result;
                  },
                  cancel: async () => { cleanup(); await reader.cancel(); },
                  releaseLock: () => {
                    cleanup(); controller.signal.removeEventListener("abort", cancel); reader.releaseLock();
                  }
                };
              };
              const value = Reflect.get(body, bodyKey, body);
              return typeof value === "function" ? value.bind(body) : value;
            }
          });
        }
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      }
    });
    attachUsageResponse(wrapped, ticket);
    return wrapped;
  } catch (error) { cleanup(); assertSessionEpoch(expected); throw error; }
}
