// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { URL as NodeURL } from "node:url";
import { retryModuleImport } from "./retryModuleImport";

afterEach(() => { document.head.innerHTML = ""; vi.restoreAllMocks(); });

// Exercise the installed production helper, including its shared `seen` cache
// and allSettled/first-error behavior, rather than mocking those semantics.
function vitePreload() {
  const source = readFileSync(new NodeURL("../node_modules/vite/dist/node/chunks/config.js", import.meta.url), "utf8");
  const start = source.indexOf("function preload(baseModule, deps, importerUrl) {");
  const end = source.indexOf("\nfunction getPreloadCode(", start);
  if (start < 0 || end < 0) throw new Error("Locate the preload helper after updating Vite");
  return new Function("document", "window", "Event", `
    const __VITE_IS_MODERN__ = true, seen = {}, scriptRel = 'modulepreload';
    const assetsURL = dep => dep;
    ${source.slice(start, end)}
    return preload;
  `)(document, window, Event) as <T>(load: () => Promise<T>, dependencies: string[]) => Promise<T>;
}

const stylesheet = (path: string) => [...document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')]
  .find(link => new URL(link.href).pathname === path)!;

describe("shared module transport", () => {
  it("shares concurrent prefetch/mount requests and releases rejected promises", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ default: "page" });
    const request = retryModuleImport(load);
    const first = request();
    expect(request()).toBe(first);
    await expect(first).rejects.toThrow("offline");
    const retry = request();
    expect(request()).toBe(retry);
    expect(await retry).toEqual({ default: "page" });
    expect(request()).toBe(retry);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("waits for a failed stylesheet to load before evaluating the page on retry", async () => {
    const old = document.createElement("link");
    old.rel = "stylesheet"; old.href = "/assets/page.css";
    old.nonce = "test-nonce";
    document.head.append(old);
    const load = vi.fn().mockRejectedValueOnce(new Error(`Unable to preload CSS for ${old.href}`))
      .mockResolvedValueOnce({ default: "styled page" });
    const request = retryModuleImport(load);
    await expect(request()).rejects.toThrow("Unable to preload CSS");
    const retry = request();
    const link = document.querySelector("link")!;
    expect(link).not.toBe(old);
    expect(link.nonce).toBe("test-nonce");
    expect(load).toHaveBeenCalledOnce();
    link.dispatchEvent(new Event("load"));
    expect(await retry).toEqual({ default: "styled page" });
  });

  it("recovers every stylesheet rejected by one real Vite preload before rendering", async () => {
    const preload = vitePreload();
    const evaluate = vi.fn(async () => ({ default: "styled page" }));
    const request = retryModuleImport(() => preload(evaluate, ["/assets/page.css", "/assets/shared.css"]));
    const healthy = document.createElement("link");
    healthy.rel = "stylesheet"; healthy.href = "/assets/healthy.css";
    document.head.append(healthy);
    const first = request();
    const page = stylesheet("/assets/page.css"), shared = stylesheet("/assets/shared.css");
    shared.nonce = "shared-nonce";
    shared.crossOrigin = "anonymous";
    page.dispatchEvent(new Event("error"));
    shared.dispatchEvent(new Event("error"));
    await expect(first).rejects.toThrow("Unable to preload CSS for /assets/page.css");

    const retry = request();
    const newPage = stylesheet("/assets/page.css"), newShared = stylesheet("/assets/shared.css");
    expect(newPage).not.toBe(page);
    expect(newShared).not.toBe(shared);
    expect(newShared.nonce).toBe("shared-nonce");
    expect(newShared.crossOrigin).toBe("anonymous");
    expect(stylesheet("/assets/healthy.css")).toBe(healthy);
    newPage.dispatchEvent(new Event("load"));
    await Promise.resolve();
    expect(evaluate).not.toHaveBeenCalled();
    newShared.dispatchEvent(new Event("load"));
    expect(await retry).toEqual({ default: "styled page" });
    expect(evaluate).toHaveBeenCalledOnce();
    expect(request()).toBe(retry);
  });

  it("retries a failed Chromium stylesheet even when its link retains a sheet", async () => {
    const preload = vitePreload();
    const evaluate = vi.fn(async () => ({ default: "styled page" }));
    const request = retryModuleImport(() => preload(evaluate, ["/assets/page.css"]));
    const initial = request();
    const failed = stylesheet("/assets/page.css");
    Object.defineProperty(failed, "sheet", { value: { cssRules: [] } });
    failed.dispatchEvent(new Event("error"));
    await expect(initial).rejects.toThrow("Unable to preload CSS");
    const retry = request();
    const replacement = stylesheet("/assets/page.css");
    expect(replacement).not.toBe(failed);
    expect(evaluate).not.toHaveBeenCalled();
    replacement.dispatchEvent(new Event("load"));
    expect(await retry).toEqual({ default: "styled page" });
  });

  it("keeps an already loaded replacement of the failed stylesheet", async () => {
    const preload = vitePreload();
    const request = retryModuleImport(() => preload(async () => ({ default: "styled page" }), ["/assets/page.css"]));
    const initial = request();
    const failed = stylesheet("/assets/page.css");
    failed.dispatchEvent(new Event("error"));
    await expect(initial).rejects.toThrow("Unable to preload CSS");
    const replacement = failed.cloneNode() as HTMLLinkElement;
    Object.defineProperty(replacement, "sheet", { value: { cssRules: [] } });
    failed.replaceWith(replacement);
    expect(await request()).toEqual({ default: "styled page" });
    expect(stylesheet("/assets/page.css")).toBe(replacement);
  });

  it("shares failed CSS recovery across concurrent page retries without replacing the active retry link", async () => {
    const preload = vitePreload();
    const firstPage = vi.fn(async () => ({ default: "first page" }));
    const secondPage = vi.fn(async () => ({ default: "second page" }));
    const first = retryModuleImport(() => preload(firstPage, ["/assets/first.css", "/assets/shared.css"]));
    const second = retryModuleImport(() => preload(secondPage, ["/assets/second.css", "/assets/shared.css"]));
    const initialFirst = first(), initialSecond = second();
    for (const path of ["/assets/first.css", "/assets/shared.css", "/assets/second.css"]) {
      stylesheet(path).dispatchEvent(new Event("error"));
    }
    await expect(initialFirst).rejects.toThrow("Unable to preload CSS");
    await expect(initialSecond).rejects.toThrow("Unable to preload CSS");
    const retryFirst = first();
    const shared = stylesheet("/assets/shared.css");
    const retrySecond = second();
    expect(stylesheet("/assets/shared.css")).toBe(shared);
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) link.dispatchEvent(new Event("load"));
    expect(await retryFirst).toEqual({ default: "first page" });
    expect(await retrySecond).toEqual({ default: "second page" });
    expect(firstPage).toHaveBeenCalledOnce();
    expect(secondPage).toHaveBeenCalledOnce();
    expect(stylesheet("/assets/shared.css")).toBe(shared);
  });

  it("retries a repeatedly failing shared stylesheet without refetching styles already recovered", async () => {
    const preload = vitePreload();
    const evaluate = vi.fn(async () => ({ default: "page" }));
    const request = retryModuleImport(() => preload(evaluate, ["/assets/page.css", "/assets/shared.css"]));
    const initial = request();
    stylesheet("/assets/page.css").dispatchEvent(new Event("error"));
    stylesheet("/assets/shared.css").dispatchEvent(new Event("error"));
    await expect(initial).rejects.toThrow("Unable to preload CSS");
    const firstRetry = request();
    const page = stylesheet("/assets/page.css");
    page.dispatchEvent(new Event("load"));
    stylesheet("/assets/shared.css").dispatchEvent(new Event("error"));
    await expect(firstRetry).rejects.toThrow("Unable to preload CSS");
    expect(evaluate).not.toHaveBeenCalled();
    const secondRetry = request();
    expect(stylesheet("/assets/page.css")).toBe(page);
    stylesheet("/assets/shared.css").dispatchEvent(new Event("load"));
    expect(await secondRetry).toEqual({ default: "page" });
    expect(evaluate).toHaveBeenCalledOnce();
  });
});
