// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const mocks = vi.hoisted(() => ({ guard: vi.fn(), knowledgeRender: vi.fn(), jobUpdate: null as ((id: string) => void) | null }));
vi.mock("./components/StructureWorkbenchPage", async () => {
  const { forwardRef, useImperativeHandle } = await import("react");
  return { StructureWorkbenchPage: forwardRef(function Workbench({ onOpenModule }: { onOpenModule: (id: string) => void }, ref) {
    useImperativeHandle(ref, () => ({ syncBeforeLeave: mocks.guard }));
    return <section><h1>结构工作台</h1><iframe title="transition retained canvas" /><button onClick={() => onOpenModule("knowledge")}>内部打开知识检索</button></section>;
  }) };
});
vi.mock("./components/KnowledgeSearch", () => ({ KnowledgeSearch: ({ initialQuery }: { initialQuery: string }) => {
  mocks.knowledgeRender();
  return <section data-testid="knowledge-view"><h1>知识检索内容</h1><span>{initialQuery}</span></section>;
} }));
vi.mock("./components/DatabaseFilterPage", () => ({ DatabaseFilterPage: () => <section data-testid="filter-view"><h1>筛选内容</h1></section> }));
vi.mock("./components/MonomerMdSimulationPage", () => ({ MonomerMdSimulationPage: ({ initialJobId, onJobIdChange }: { initialJobId: string; onJobIdChange: (id: string) => void }) => {
  mocks.jobUpdate = onJobIdChange;
  return <section><span data-testid="job-id">{initialJobId}</span><button onClick={() => onJobIdChange("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")}>更新任务 URL</button></section>;
} }));

let animations: { onfinish: (() => void) | null; oncancel: (() => void) | null; cancel: ReturnType<typeof vi.fn> }[];
let original: PropertyDescriptor | undefined;
beforeEach(() => {
  vi.useFakeTimers(); animations = [];
  window.history.replaceState({}, "", "/structure-workbench");
  mocks.guard.mockReset().mockResolvedValue(undefined);
  mocks.knowledgeRender.mockClear();
  mocks.jobUpdate = null;
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((fn) => window.setTimeout(() => fn(performance.now()), 16));
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => window.clearTimeout(id));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: vi.fn(() => {
    const instance = { onfinish: null, oncancel: null, cancel: vi.fn() };
    animations.push(instance); return instance;
  }) });
});
afterEach(() => {
  cleanup();
  if (original) Object.defineProperty(HTMLElement.prototype, "animate", original);
  else Reflect.deleteProperty(HTMLElement.prototype, "animate");
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
const tick = (ms: number) => act(() => vi.advanceTimersByTime(ms));
const end = () => act(() => animations.at(-1)?.onfinish?.());
const discover = () => {
  const button = screen.getByRole("button", { name: "材料发现 Discover" });
  if (button.getAttribute("aria-expanded") === "false") fireEvent.click(button);
};

describe("App serial module integration", () => {
  it("guards internal navigation once, commits URL/children only after exit and retains the canvas", async () => {
    const view = render(<App />);
    const iframe = screen.getByTitle("transition retained canvas");
    const content = view.container.querySelector<HTMLElement>("[data-module-content]")!;
    fireEvent.click(screen.getByRole("button", { name: "内部打开知识检索" }));
    await act(async () => {});
    expect(mocks.guard).toHaveBeenCalledOnce();
    expect(window.location.pathname).toBe("/structure-workbench");
    expect(screen.queryByTestId("knowledge-view")).toBeNull();
    expect(content.dataset.modulePhase).toBe("exiting");
    tick(400); end();
    expect(window.location.pathname).toBe("/knowledge");
    expect(screen.getByTestId("knowledge-view")).not.toBeNull();
    expect(content.style.opacity).toBe("0");
    expect(content.hasAttribute("inert")).toBe(true);
    expect(screen.getByTitle("transition retained canvas")).toBe(iframe);
    const rendersAfterCommit = mocks.knowledgeRender.mock.calls.length;
    tick(232); tick(400); end();
    expect(content.dataset.modulePhase).toBe("idle");
    expect(content.hasAttribute("inert")).toBe(false);
    expect(screen.getByRole("heading", { name: "知识检索内容" })).not.toBeNull();
    expect(mocks.knowledgeRender).toHaveBeenCalledTimes(rendersAfterCommit);
  });

  it("replaces a just-pushed unseen URL in the blank phase, with no second exit", async () => {
    const view = render(<App />); discover();
    const push = vi.spyOn(window.history, "pushState");
    const replace = vi.spyOn(window.history, "replaceState");
    fireEvent.click(screen.getByRole("button", { name: "数据库筛选" }));
    await act(async () => {}); end();
    expect(window.location.pathname).toBe("/database-filter");
    expect(view.container.querySelector("[data-module-content]")?.getAttribute("data-module-phase")).toBe("blank");
    fireEvent.click(screen.getByRole("button", { name: "知识检索" }));
    expect(window.location.pathname).toBe("/knowledge");
    expect(push).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledOnce();
    expect(animations).toHaveLength(1);
  });

  it("coalesces history snapshots and defers knowledge query props until the transparent commit", async () => {
    let resolve!: () => void;
    mocks.guard.mockImplementation(() => new Promise<void>((done) => { resolve = done; }));
    render(<App />); discover();
    const push = vi.spyOn(window.history, "pushState");
    fireEvent.click(screen.getByRole("button", { name: "数据库筛选" }));
    window.history.replaceState({}, "", "/knowledge?q=first");
    fireEvent(window, new PopStateEvent("popstate"));
    window.history.replaceState({}, "", "/knowledge?q=latest");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.queryByTestId("knowledge-view")).toBeNull();
    await act(async () => resolve());
    expect(mocks.guard).toHaveBeenCalledOnce();
    end();
    expect(screen.getByTestId("knowledge-view").textContent).toContain("latest");
    expect(push).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?q=latest");
  });

  it("updates a same-module task URL without a full-page animation", () => {
    window.history.replaceState({}, "", "/monomer-md-simulation?job=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "更新任务 URL" }));
    expect(screen.getByTestId("job-id").textContent).toBe("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(window.location.search).toContain("job=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(animations).toHaveLength(0);
  });

  it("replaces the unseen entry when a newly mounted module reports its task ID during the blank", async () => {
    const view = render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "材料设计 Build" }));
    const push = vi.spyOn(window.history, "pushState");
    const replace = vi.spyOn(window.history, "replaceState");
    fireEvent.click(screen.getByRole("button", { name: "单体 MD 模拟" }));
    await act(async () => {}); end();
    const content = view.container.querySelector<HTMLElement>("[data-module-content]")!;
    expect(content.dataset.modulePhase).toBe("blank");
    expect(content.style.opacity).toBe("0");
    expect(mocks.jobUpdate).not.toBeNull();
    act(() => mocks.jobUpdate!("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));
    expect(push).toHaveBeenCalledOnce();
    expect(replace).toHaveBeenCalledExactlyOnceWith({ module: "monomerMdSimulation", datasetKey: null }, "", "/monomer-md-simulation?job=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(screen.getByTestId("job-id").textContent).toBe("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(animations).toHaveLength(1);
    tick(232); end();
    act(() => mocks.jobUpdate!("cccccccccccccccccccccccccccccccc"));
    expect(push).toHaveBeenCalledTimes(2);
    expect(replace).toHaveBeenCalledOnce();
    expect(content.dataset.modulePhase).toBe("idle");
    expect(animations).toHaveLength(2);
  });
});
