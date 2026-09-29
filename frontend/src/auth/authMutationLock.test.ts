import { afterEach, expect, it, vi } from "vitest";
import { withAuthMutationLock } from "./authMutationLock";

afterEach(() => vi.unstubAllGlobals());
it("does not begin a queued operation that is already cancelled", async () => {
  const request = vi.fn(), work = vi.fn();
  vi.stubGlobal("navigator", { locks: { request } });
  const controller = new AbortController(); controller.abort();
  await expect(withAuthMutationLock(work, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  expect(request).not.toHaveBeenCalled(); expect(work).not.toHaveBeenCalled();
});
it("does not send credentials when opening browser storage fails", async () => {
  vi.stubGlobal("navigator", {});
  vi.stubGlobal("indexedDB", { open: () => { throw new DOMException("storage disabled", "SecurityError"); } });
  const work = vi.fn();
  await expect(withAuthMutationLock(work)).rejects.toThrow("请允许此站点使用浏览器存储");
  expect(work).not.toHaveBeenCalled();
});
it("does not fall back to a second locking mechanism when a Web Lock request fails", async () => {
  const open = vi.fn(), work = vi.fn();
  vi.stubGlobal("indexedDB", { open });
  vi.stubGlobal("navigator", { locks: { request: () => Promise.reject(new DOMException("cancelled", "AbortError")) } });
  await expect(withAuthMutationLock(work)).rejects.toMatchObject({ name: "AbortError" });
  expect(work).not.toHaveBeenCalled(); expect(open).not.toHaveBeenCalled();
});
