// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ preload: vi.fn(), page: vi.fn(), mount: vi.fn() }));
vi.mock("@structure-editor-preload", () => ({ preloadStructureEditor: mocks.preload }));
vi.mock("./pages", () => ({ preloadPage: mocks.page }));
vi.mock("./mountApp", () => ({ mountApp: mocks.mount }));

beforeEach(() => {
  vi.resetModules();
  mocks.preload.mockReset().mockReturnValue(new Promise(() => {}));
  mocks.mount.mockReset();
  mocks.page.mockReset().mockResolvedValue(undefined);
  document.body.innerHTML = '<div id="root"></div>';
});
afterEach(() => { vi.restoreAllMocks(); });

describe("refresh bootstrap", () => {
  it.each(["/structure-workbench", "/database-query", "/explorer", "/homopolymer-property-prediction", "/conditional-generation", "/reverse-design/"])("prefetches %s without waiting for SDK transport to mount the app", async path => {
    window.history.replaceState({}, "", path);
    await import("./main");
    await vi.dynamicImportSettled();
    expect(mocks.preload).toHaveBeenCalledOnce();
    expect(mocks.mount).toHaveBeenCalledOnce();
    expect(mocks.preload.mock.invocationCallOrder[0]).toBeLessThan(mocks.mount.mock.invocationCallOrder[0]);
  });

  it.each(["/", "/knowledge", "/monomer-dft", "/conditional-generation/polytao", "/database/property-filter"])("keeps SDK absent on %s", async path => {
    window.history.replaceState({}, "", path);
    await import("./main");
    await vi.dynamicImportSettled();
    expect(mocks.preload).not.toHaveBeenCalled();
    expect(mocks.mount).toHaveBeenCalledOnce();
  });

  it("leaves failed SDK prefetch recovery to the visible editor", async () => {
    window.history.replaceState({}, "", "/structure-workbench");
    mocks.preload.mockRejectedValue(new Error("SDK network failure"));
    await import("./main");
    await vi.dynamicImportSettled();
    expect(mocks.mount).toHaveBeenCalledOnce();
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });
});
