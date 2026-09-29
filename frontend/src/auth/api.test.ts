import { afterEach, expect, it, vi } from "vitest";
import { authRequest } from "./api";
import { installSession, retireSession } from "./session";
afterEach(() => vi.unstubAllGlobals());
it("fails closed when neither Web Locks nor IndexedDB can serialize credentials", async () => {
  vi.stubGlobal("navigator", {});
  vi.stubGlobal("indexedDB", undefined);
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await expect(authRequest("login", { username: "a", password: "private-password" })).rejects.toThrow("请允许此站点使用浏览器存储");
  await expect(authRequest("logout", {})).rejects.toThrow("请允许此站点使用浏览器存储");
  await expect(authRequest("password", {})).rejects.toThrow("请允许此站点使用浏览器存储");
  expect(fetch).not.toHaveBeenCalled();
});
it("still permits read-only identity checks without Web Locks", async () => {
  vi.stubGlobal("navigator", {});
  const fetch = vi.fn().mockResolvedValue(new Response('{"authenticated":false}')); vi.stubGlobal("fetch", fetch);
  await expect(authRequest("session")).resolves.toMatchObject({ authenticated: false });
  expect(fetch).toHaveBeenCalledOnce();
});
it("serializes Cookie mutations through the browser auth lock", async () => {
  const request = vi.fn((_name, _options, callback) => callback());
  vi.stubGlobal("navigator", { locks: { request } });
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 })); vi.stubGlobal("fetch", fetch);
  await authRequest("logout", {});
  expect(request).toHaveBeenCalledWith("nexpoly-auth-mutation", expect.any(Object), expect.any(Function));
  expect(fetch.mock.calls[0][1].headers.get("X-Session-Context")).toBe("test-session");
});
it("does not let an old queued logout acquire account B's credentials", async () => {
  let execute!: () => void;
  vi.stubGlobal("navigator", { locks: { request: (_name: string, _options: unknown, callback: () => Promise<unknown>) => new Promise((resolve, reject) => {
    execute = () => { callback().then(resolve, reject); };
  }) } });
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  const pending = authRequest("logout", {});
  retireSession(); installSession({ authenticated: true, user: { id: "b", username: "b", must_change_password: false }, session_id: "session-b", csrf_token: "csrf-b", capabilities: {} });
  execute();
  await expect(pending).rejects.toMatchObject({ name: "AbortError" }); expect(fetch).not.toHaveBeenCalled();
});
it("drains a dispatched credential response before releasing the lock after cancellation", async () => {
  const order: string[] = [];
  vi.stubGlobal("navigator", { locks: { request: async (_name: string, _options: unknown, callback: () => Promise<unknown>) => {
    try { return await callback(); } finally { order.push("release"); }
  } } });
  let respond!: (response: Response) => void;
  const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>(resolve => { respond = resolve; })); vi.stubGlobal("fetch", fetch);
  const controller = new AbortController();
  const pending = authRequest("logout", {}, controller.signal);
  const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
  controller.abort();
  await Promise.resolve();
  expect(order).toEqual([]);
  expect(fetch.mock.calls[0][1]?.signal).toBeUndefined();
  order.push("response"); respond(new Response(null, { status: 204 }));
  await rejected;
  expect(order).toEqual(["response", "release"]);
});
it("discards a late failure after the initiating session was retired", async () => {
  let respond!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { respond = resolve; })));
  const pending = authRequest("session");
  retireSession();
  respond(new Response('{"detail":"old account error"}', { status: 401 }));
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
});
