/** Browser identity fence shared by JSON, downloads, SSE and local async writes. */
export type AuthSession = {
  authenticated: boolean;
  user: { id: string; username: string; must_change_password: boolean } | null;
  session_id: string | null;
  csrf_token: string | null;
  capabilities: Record<string, boolean>;
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
  ++epoch;
  for (const request of requests) request.abort();
  requests.clear();
  for (const listener of retirementListeners) listener();
}
export function installSession(next: AuthSession) {
  session = next;
  ready = true;
}

/** Auth endpoints use the raw transport so a guest can establish a session. */
export async function privateFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  if (!ready) throw staleSessionError();
  if (!session.authenticated || !session.session_id) throw new Error("请登录账号。");
  const expected = epoch;
  const controller = new AbortController();
  const sourceRequest = typeof Request !== "undefined" && input instanceof Request ? input : null;
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
    const response = await fetch(input, { ...init, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    check();
    let invalidIdentity = response.status === 401;
    if (response.status === 409 || response.status === 403) {
      const failure = await response.clone().json().catch(() => null);
      check();
      invalidIdentity = ["session_context_mismatch", "password_change_required"].includes(failure?.code);
    }
    if (invalidIdentity) {
      // Check the originating epoch before dispatch: a late A/401 must not log B out.
      retireSession();
      for (const listener of invalidationListeners) listener();
      throw staleSessionError();
    }
    if ([204, 205, 304].includes(response.status) || method === "HEAD") cleanup();
    return new Proxy(response, {
      get(target, key) {
        if (["json", "text", "blob", "arrayBuffer", "formData"].includes(String(key))) {
          return async () => {
            try {
              check();
              const result = await (target[key as keyof Response] as () => Promise<unknown>).call(target);
              check();
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
  } catch (error) { cleanup(); assertSessionEpoch(expected); throw error; }
}
