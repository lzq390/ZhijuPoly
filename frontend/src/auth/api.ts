import { type AuthSession, assertSessionEpoch, getSession, getSessionEpoch } from "./session";
import { withAuthMutationLock } from "./authMutationLock";
const AUTH_URL = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/auth`;
export async function authRequest(path: string, body?: unknown, signal?: AbortSignal): Promise<AuthSession | null> {
  const expected = getSessionEpoch();
  const current = getSession();
  const check = () => {
    assertSessionEpoch(expected);
    if (signal?.aborted) throw new DOMException("账号操作已取消。", "AbortError");
  };
  const send = async () => {
    // A queued action must retain its original identity, even when another tab
    // acquired the browser auth lock and changed the shared Cookie first.
    check();
    const headers = new Headers({ Accept: "application/json" });
    if (body !== undefined) headers.set("Content-Type", "application/json");
    if (current.session_id) headers.set("X-Session-Context", current.session_id);
    if (current.csrf_token) headers.set("X-CSRF-Token", current.csrf_token);
    const response = await fetch(`${AUTH_URL}/${path}`, {
      method: body === undefined ? "GET" : "POST", headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", cache: "no-store",
      // An aborted UI must not release the origin lock while a dispatched
      // credential request could still deliver Set-Cookie. Drain it first.
      signal: body === undefined ? signal : undefined
    });
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      check();
      throw new Error(typeof error?.detail === "string" ? error.detail : `登录服务暂不可用 (${response.status})`);
    }
    if (response.status === 204) { check(); return null; }
    const value = await response.json() as AuthSession;
    check();
    if (typeof value.authenticated !== "boolean" || value.authenticated && (!value.user?.id || !value.session_id || !value.csrf_token)) {
      throw new Error("登录服务返回了无效的会话信息。");
    }
    return value;
  };
  // Serialize Cookie mutations across tabs. A session epoch cannot prevent the
  // browser from applying a late Set-Cookie response from a concurrent login.
  try {
    return await (body !== undefined ? withAuthMutationLock(send, signal) : send());
  } catch (error) { check(); throw error; }
}
