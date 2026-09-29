import { afterEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION, getSession, installSession, onSessionInvalidated, privateFetch, retireSession, type AuthSession } from "./session";
function account(id: string): AuthSession {
  return { authenticated: true, user: { id, username: id, must_change_password: false }, session_id: `session-${id}`, csrf_token: `csrf-${id}`, capabilities: {} };
}
function switchTo(id: string) { retireSession(); installSession(account(id)); }
afterEach(() => vi.unstubAllGlobals());
describe("browser account fence", () => {
  it("rejects guest requests before the transport is invoked", async () => {
    retireSession(); installSession(GUEST_SESSION); const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(privateFetch("/api/v1/predict", { method: "POST" })).rejects.toThrow("请登录账号。");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("binds JSON and multipart to the current identity without replacing multipart content type", async () => {
    switchTo("a"); const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response("{}"))); vi.stubGlobal("fetch", fetch);
    await (await privateFetch("/api/v1/predict", { method: "POST", headers: { "Content-Type": "application/json", "X-Session-Context": "forged" }, body: "{}" })).json();
    const json = fetch.mock.calls[0][1];
    expect(json.credentials).toBe("same-origin"); expect(json.cache).toBe("no-store");
    expect(json.headers.get("X-Session-Context")).toBe("session-a"); expect(json.headers.get("X-CSRF-Token")).toBe("csrf-a");
    expect(json.headers.get("Content-Type")).toBe("application/json");
    await (await privateFetch("/api/v1/import", { method: "POST", body: new FormData() })).json();
    expect(fetch.mock.calls[1][1].headers.has("Content-Type")).toBe(false);
  });
  it("aborts outstanding transport on identity retirement and propagates caller aborts", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}")); vi.stubGlobal("fetch", fetch);
    const source = new AbortController(); await privateFetch("/api/v1/private", { signal: source.signal });
    source.abort(); expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    await privateFetch("/api/v1/private"); retireSession(); expect(fetch.mock.calls[1][1].signal.aborted).toBe(true);
  });
  it("discards a response that arrives after account B has logged in", async () => {
    switchTo("a"); let resolve!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(done => { resolve = done; })));
    const result = privateFetch("/api/v1/private"); switchTo("b"); resolve(new Response('{"secret":"a"}'));
    await expect(result).rejects.toMatchObject({ name: "AbortError" }); expect(getSession().user?.id).toBe("b");
  });
  for (const method of ["json", "blob"] as const) it(`discards late ${method} body decoding across accounts`, async () => {
    switchTo("a"); let resolve!: (value: unknown) => void;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 200, [method]: () => new Promise(done => { resolve = done; }) }));
    const response = await privateFetch("/api/v1/private"); const result = response[method]();
    switchTo("b"); resolve(method === "json" ? { secret: "a" } : new Blob(["a"]));
    await expect(result).rejects.toMatchObject({ name: "AbortError" });
  });
  it("does not let a late 401 from A invalidate B", async () => {
    switchTo("a"); let resolve!: (value: Response) => void; const invalidate = vi.fn(); const unsubscribe = onSessionInvalidated(invalidate);
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(done => { resolve = done; })));
    const result = privateFetch("/api/v1/private"); switchTo("b"); resolve(new Response("", { status: 401 }));
    await expect(result).rejects.toMatchObject({ name: "AbortError" }); expect(invalidate).not.toHaveBeenCalled(); unsubscribe();
  });
  it("invalidates the active identity on its own 401", async () => {
    const invalidate = vi.fn(); const unsubscribe = onSessionInvalidated(invalidate);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
    await expect(privateFetch("/api/v1/private")).rejects.toMatchObject({ name: "AbortError" });
    expect(invalidate).toHaveBeenCalledOnce(); unsubscribe();
  });
  it("revalidates a stale tab when the backend reports session-context mismatch", async () => {
    const invalidate = vi.fn(); const unsubscribe = onSessionInvalidated(invalidate);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"code":"session_context_mismatch"}', { status: 409 })));
    await expect(privateFetch("/api/v1/private")).rejects.toMatchObject({ name: "AbortError" });
    expect(invalidate).toHaveBeenCalledOnce(); unsubscribe();
  });
  it("stops an open SSE reader and discards its pending chunk on switch", async () => {
    switchTo("a"); let source!: ReadableStreamDefaultController<Uint8Array>; const cancelled = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new ReadableStream({ start(controller) { source = controller; }, cancel: cancelled }))));
    const reader = (await privateFetch("/api/v1/stream")).body!.getReader();
    source.enqueue(new TextEncoder().encode("data: one\n\n")); expect((await reader.read()).done).toBe(false);
    const next = reader.read(); switchTo("b");
    await expect(next).rejects.toMatchObject({ name: "AbortError" }); expect(cancelled).toHaveBeenCalledOnce(); reader.releaseLock();
  });
});
