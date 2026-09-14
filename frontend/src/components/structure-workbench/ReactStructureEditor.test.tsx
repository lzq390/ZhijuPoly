// @vitest-environment jsdom
import { StrictMode, useEffect, useSyncExternalStore } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StructureWorkspace } from "../../structure/workspace";
import type { NativeKetcher } from "../../structure/nativeSession";
import ReactStructureEditor, { type RuntimeProps } from "./ReactStructureEditor";

const runtime = vi.hoisted(() => ({ mounts: new Map<RuntimeProps["session"], RuntimeProps>(), load: vi.fn(), render: vi.fn() }));
vi.mock("./loadKetcherRuntime", () => ({ loadKetcherRuntime: runtime.load }));
function Runtime(props: RuntimeProps) {
  runtime.render();
  useEffect(() => { runtime.mounts.set(props.session, props); }, [props]);
  return <div data-testid="mock-sdk" />;
}

let sized = true;
const resizes = new Set<ResizeObserverCallback>();
function Shell({ workspace }: { workspace: StructureWorkspace }) {
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  return <div data-structure-editor data-editor-status={state.status} inert={state.status === "loading"}>
    <ReactStructureEditor key={state.mountKey} workspace={workspace} title="test editor" />
  </div>;
}
function fixture(phase = "idle") {
  const module = document.createElement("div");
  module.dataset.moduleContent = "structureWorkbench";
  module.dataset.modulePhase = phase;
  if (phase !== "idle") {
    module.dataset.moduleTransitioning = "true";
    module.setAttribute("aria-hidden", "true");
    module.setAttribute("inert", "");
  }
  const layer = document.createElement("div");
  const mount = document.createElement("div");
  layer.append(mount);
  module.append(layer);
  document.body.append(module);
  const workspace = new StructureWorkspace("CCO");
  return { module, layer, workspace, mount,
    render: () => render(<StrictMode><Shell workspace={workspace} /></StrictMode>, { container: mount }) };
}
function sdkFixture() {
  let smiles = "";
  const changes = new Set<() => void>();
  const failures = new Set<() => void>();
  const sdk: NativeKetcher = {
    getSmiles: vi.fn(async () => smiles),
    getKet: vi.fn(async () => JSON.stringify({ root: { nodes: smiles ? [{ $ref: "mol0" }] : [] },
      mol0: { type: "molecule", atoms: smiles ? [{ label: "C" }, { label: "C" }, { label: "O" }] : [] } })),
    setMolecule: vi.fn(async (source: string) => { smiles = source ? "CCO" : ""; }),
    changeEvent: { add: vi.fn(listener => changes.add(listener)), remove: vi.fn(listener => changes.delete(listener)) },
    eventBus: { on: (_event, listener) => failures.add(listener), off: (_event, listener) => failures.delete(listener) },
    editor: { centerViewportAccordingToStruct: vi.fn() },
    retire: vi.fn(() => { sdk.disposed = true; })
  };
  return { sdk, changes, failures };
}
async function flush(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}
async function imports() {
  await act(async () => { await vi.dynamicImportSettled(); });
  await flush(1);
}
function latest() { return [...runtime.mounts.values()].at(-1)!; }
async function initialize(sdk: NativeKetcher) {
  await act(async () => { latest().onInit(sdk); });
  await flush(700);
}
async function unlock(module: HTMLElement) {
  await act(async () => {
    module.dataset.modulePhase = "idle";
    module.removeAttribute("data-module-transitioning");
    module.removeAttribute("aria-hidden");
    module.removeAttribute("inert");
  });
}

beforeEach(() => {
  runtime.load.mockReset().mockResolvedValue({ default: Runtime });
  runtime.render.mockClear();
  vi.useFakeTimers();
  runtime.mounts.clear();
  sized = true;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, sized ? 900 : 0, sized ? 600 : 0));
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: ResizeObserverCallback) { resizes.add(callback); }
    observe() {}
    disconnect() { resizes.delete(this.callback); }
    unobserve() {}
  });
});
afterEach(() => {
  cleanup();
  expect(resizes.size).toBe(0);
  expect(vi.getTimerCount()).toBe(0);
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("native editor preparation during module motion", () => {
  it.each(["blank", "entering"])("restores during %s without unlocking or centering the canvas", async phase => {
    const f = fixture(phase);
    const { sdk, changes } = sdkFixture();
    const view = f.render();
    await imports();
    expect(runtime.mounts.size).toBe(1);
    const renders = runtime.render.mock.calls.length;
    await initialize(sdk);
    expect(runtime.render).toHaveBeenCalledTimes(renders);
    expect(f.workspace.getSnapshot().status).toBe("ready");
    expect(changes.size).toBe(1);
    expect(sdk.changeEvent!.add).toHaveBeenCalledOnce();
    expect(f.module.getAttribute("aria-hidden")).toBe("true");
    expect(f.module.hasAttribute("inert")).toBe(true);
    expect(sdk.editor!.centerViewportAccordingToStruct).not.toHaveBeenCalled();
    await unlock(f.module);
    await flush(249);
    expect(sdk.editor!.centerViewportAccordingToStruct).not.toHaveBeenCalled();
    await flush(1);
    expect(sdk.editor!.centerViewportAccordingToStruct).toHaveBeenCalledOnce();
    await act(async () => { f.module.style.opacity = "1"; });
    await flush(10);
    expect(runtime.mounts.size).toBe(1);
    expect(sdk.changeEvent!.add).toHaveBeenCalledOnce();
    view.unmount();
    expect(changes.size).toBe(0);
  });

  it.each(["zero-size", "hidden", "nested-aria", "hidden-2d", "ordinary-aria", "exiting", "outer-locked-module"])(
    "defers %s, without starting its timeout, then initializes once when shown", async kind => {
      const f = fixture(kind === "exiting" ? "exiting" : "blank");
      if (kind === "zero-size") sized = false;
      if (kind === "hidden") f.layer.hidden = true;
      if (["nested-aria", "hidden-2d"].includes(kind)) f.layer.setAttribute("aria-hidden", "true");
      if (kind === "ordinary-aria") f.module.removeAttribute("data-module-transitioning");
      if (kind === "outer-locked-module") f.layer.dataset.moduleContent = "nested";
      f.render();
      await imports();
      await flush(20000);
      expect(runtime.mounts.size).toBe(0);
      expect(runtime.load).not.toHaveBeenCalled();
      expect(f.workspace.getSnapshot().status).toBe("loading");
      await act(async () => {
        sized = true;
        f.layer.hidden = false;
        f.layer.removeAttribute("aria-hidden");
        delete f.layer.dataset.moduleContent;
        f.module.dataset.modulePhase = "entering";
        f.module.dataset.moduleTransitioning = "true";
        resizes.forEach(callback => callback([], {} as ResizeObserver));
      });
      await imports();
      const { sdk } = sdkFixture();
      await initialize(sdk);
      expect(runtime.mounts.size).toBe(1);
      expect(f.workspace.getSnapshot().status).toBe("ready");
      expect(sdk.setMolecule).toHaveBeenCalledTimes(2); // clear then the original SMILES
      expect(sdk.changeEvent!.add).toHaveBeenCalledOnce();
    }
  );

  it("holds a late onInit while hidden and cancels centering when relocked", async () => {
    const f = fixture();
    f.render();
    await imports();
    await act(async () => { f.layer.setAttribute("aria-hidden", "true"); });
    const { sdk } = sdkFixture();
    await initialize(sdk);
    expect(sdk.setMolecule).not.toHaveBeenCalled();
    expect(f.workspace.getSnapshot().status).toBe("loading");
    await act(async () => { f.layer.removeAttribute("aria-hidden"); });
    await flush(400);
    expect(f.workspace.getSnapshot().status).toBe("ready");
    await act(async () => { f.module.setAttribute("inert", ""); });
    await flush(500);
    expect(sdk.editor!.centerViewportAccordingToStruct).not.toHaveBeenCalled();
    await act(async () => { f.module.removeAttribute("inert"); });
    await flush(250);
    expect(sdk.editor!.centerViewportAccordingToStruct).toHaveBeenCalledOnce();
  });

  it("retires a late old SDK after immediate remount, without restoring into the new session", async () => {
    const f = fixture("entering");
    const view = f.render();
    await imports();
    const old = latest();
    const oldService = { destroy: vi.fn() };
    old.session.ownService(oldService);
    await act(async () => { f.workspace.retryEditor(); });
    await imports();
    expect(old.session.signal.aborted).toBe(true);
    expect(oldService.destroy).toHaveBeenCalledOnce();
    const late = sdkFixture().sdk;
    await act(async () => { old.onInit(late); });
    expect(late.retire).toHaveBeenCalledOnce();
    expect(late.setMolecule).not.toHaveBeenCalled();
    const { sdk, changes } = sdkFixture();
    await initialize(sdk);
    expect(f.workspace.getSnapshot().status).toBe("ready");
    expect(changes.size).toBe(1);
    view.unmount();
    expect(changes.size).toBe(0);
  });

  it("discards an import continuation after unmount", async () => {
    let finish!: (value: { default: typeof Runtime }) => void;
    const transport = new Promise<{ default: typeof Runtime }>(resolve => { finish = resolve; });
    runtime.load.mockReturnValue(transport);
    const f = fixture("blank");
    const view = f.render();
    view.unmount();
    await act(async () => { finish({ default: Runtime }); });
    await imports();
    expect(runtime.mounts.size).toBe(0);
    expect(f.workspace.getSnapshot().status).toBe("unmounted");
  });

  it("allows an in-flight module to load while hidden but waits to restore", async () => {
    let finish!: (value: { default: typeof Runtime }) => void;
    const transport = new Promise<{ default: typeof Runtime }>(resolve => { finish = resolve; });
    runtime.load.mockReturnValue(transport);
    const f = fixture("blank");
    f.render();
    await act(async () => { f.layer.hidden = true; finish({ default: Runtime }); });
    await imports();
    const { sdk } = sdkFixture();
    await initialize(sdk);
    expect(sdk.setMolecule).not.toHaveBeenCalled();
    await act(async () => { f.layer.hidden = false; });
    await flush(700);
    expect(sdk.changeEvent!.add).toHaveBeenCalledOnce();
    expect(f.workspace.getSnapshot().status).toBe("ready");
  });

  it("retries rejected SDK transport without losing the shared draft", async () => {
    runtime.load.mockRejectedValue(new Error("test network failure"));
    const f = fixture("blank");
    f.render();
    await imports();
    expect(f.workspace.getSnapshot()).toMatchObject({ status: "error", draft: "CCO" });
    expect(vi.getTimerCount()).toBe(0);
    runtime.load.mockResolvedValue({ default: Runtime });
    await act(async () => { f.workspace.retryEditor(); });
    await imports();
    await initialize(sdkFixture().sdk);
    expect(f.workspace.getSnapshot()).toMatchObject({ status: "ready", draft: "CCO" });
  });

  it("retries an initialization failure with the original document and a fresh session", async () => {
    const f = fixture("entering");
    f.render();
    await imports();
    const failed = latest();
    await act(async () => { failed.session.fail("test initialization failure"); });
    expect(f.workspace.getSnapshot()).toMatchObject({ status: "error", draft: "CCO", smiles: "CCO" });
    await act(async () => { f.workspace.retryEditor(); });
    await imports();
    expect(latest().session).not.toBe(failed.session);
    const { sdk } = sdkFixture();
    await initialize(sdk);
    expect(f.workspace.getSnapshot()).toMatchObject({ status: "ready", draft: "CCO", smiles: "CCO" });
  });
});
