// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ preload: vi.fn(), page: vi.fn(), mount: vi.fn(), simulation: vi.fn() }));
vi.mock("@structure-editor-preload", () => ({ preloadStructureEditor: mocks.preload }));
vi.mock("./pages", () => ({ preloadPage: mocks.page }));
vi.mock("./mountSimulationPayment", () => ({ mountSimulationPayment: mocks.simulation }));
vi.mock("./mountApp", () => ({ mountApp: mocks.mount }));

beforeEach(() => {
  vi.resetModules();
  mocks.preload.mockReset().mockReturnValue(new Promise(() => {}));
  mocks.mount.mockReset();
  mocks.simulation.mockReset();
  mocks.page.mockReset().mockResolvedValue(undefined);
  document.body.innerHTML = '<div id="root"></div>';
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); });

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

it.each(["/billing/simulate-pay", "/billing/simulate-pay/"])("mounts public %s without app, page graph or editor preload", async path => {
  window.history.replaceState({}, "", `${path}#token=fragment-only`);
  await import("./main"); await vi.dynamicImportSettled();
  expect(mocks.simulation).toHaveBeenCalledOnce(); expect(mocks.mount).not.toHaveBeenCalled();
  expect(mocks.preload).not.toHaveBeenCalled(); expect(mocks.page).not.toHaveBeenCalled();
  expect(document.head.querySelector('meta[name="referrer"]')?.getAttribute("content")).toBe("no-referrer");
});

it.each(["/billing/simulate-pay", "/billing/simulate-pay/"])("clears disabled %s before mounting the normal account app", async path => {
  vi.stubEnv("VITE_SUBSCRIPTION_ENABLED", "false");
  window.history.replaceState({}, "", `${path}?old=order#token=fragment-only`);
  await import("./main"); await vi.dynamicImportSettled();
  expect(window.location.pathname + window.location.search + window.location.hash).toBe("/");
  expect(mocks.simulation).not.toHaveBeenCalled();
  expect(mocks.mount).toHaveBeenCalledOnce();
  expect(mocks.preload).not.toHaveBeenCalled();
  expect(mocks.page).toHaveBeenCalledWith("home");
});

it("keeps the normal account app available when global stylesheet transport fails", async () => {
  window.history.replaceState({}, "", "/account/subscription");
  vi.doMock("./index.css", () => { throw new Error("Stylesheet unavailable"); });
  try {
    await import("./main"); await vi.dynamicImportSettled();
    expect(mocks.mount).toHaveBeenCalledOnce(); expect(document.querySelector('[role="alert"]')).toBeNull();
  } finally { vi.doUnmock("./index.css"); }
});
