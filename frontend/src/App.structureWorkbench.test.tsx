/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const mocks = vi.hoisted(() => ({
  syncSmilesFromCanvas: vi.fn(),
  fileInputRef: { current: null as HTMLInputElement | null }
}));

vi.mock("./hooks/useTgStructureCanvas", () => ({
  useTgStructureCanvas: () => ({
    fileInputRef: mocks.fileInputRef,
    handleEditorLoad: vi.fn(),
    isEditorReady: true,
    isFlipped: false,
    isFlipping: false,
    isImportingImage: false,
    isLoadingStructure: false,
    isClearing: false,
    isSyncing: false,
    isBusy: false,
    feedback: null,
    setFeedback: vi.fn(),
    copyState: "idle",
    smilesDraft: "",
    smilesDraftState: "synced",
    smilesDraftError: null,
    updateSmilesDraft: vi.fn(),
    flushSmilesDraft: vi.fn().mockResolvedValue(true),
    cancelSmilesDraftSync: vi.fn().mockResolvedValue(undefined),
    adoptCanvasSmiles: vi.fn(),
    loadStructure: vi.fn().mockResolvedValue(true),
    applyTextStructure: vi.fn().mockImplementation(async (value: string) => ({ applied: true, smiles: value })),
    clearCanvas: vi.fn().mockResolvedValue(true),
    importImageFile: vi.fn().mockResolvedValue(true),
    syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
    resolveSmilesForSearch: vi.fn().mockResolvedValue("*CC*"),
    toggle3D: vi.fn().mockResolvedValue(true),
    copySmiles: vi.fn()
  })
}));

vi.mock("./components/StructurePreview3D", () => ({
  StructurePreview3D: () => <div data-testid="structure-3d" />
}));
vi.mock("./components/DatabaseFilterPage", () => ({
  DatabaseFilterPage: () => <div data-testid="database-filter">数据库筛选</div>
}));
vi.mock("./components/DatabaseQueryPage", () => ({
  DatabaseQueryPage: () => <div data-testid="database-query">数据库查询</div>
}));
vi.mock("./components/KnowledgeSearch", () => ({
  KnowledgeSearch: () => <div data-testid="knowledge">知识检索</div>
}));

function structureIframe(container: HTMLElement) {
  return container.querySelector<HTMLIFrameElement>('iframe[title="结构工作台结构编辑器"]');
}

function openDiscoverGroup() {
  const group = screen.getByRole("button", { name: "材料发现 Discover" });
  if (group.getAttribute("aria-expanded") !== "true") fireEvent.click(group);
}

function openBuildGroup() {
  const group = screen.getByRole("button", { name: "材料设计 Build" });
  if (group.getAttribute("aria-expanded") !== "true") fireEvent.click(group);
}

beforeEach(() => {
  window.history.replaceState({}, "", "/structure-workbench");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  mocks.syncSmilesFromCanvas.mockReset().mockResolvedValue("");
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    }))
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("App 结构工作台挂载与导航", () => {
  it("冷启动深链可直接加载，普通模块往返保留同一 iframe", async () => {
    const view = render(<App />);
    const firstIframe = structureIframe(view.container);
    expect(firstIframe).not.toBeNull();

    openDiscoverGroup();
    fireEvent.click(screen.getByRole("button", { name: "数据库筛选" }));
    await screen.findByTestId("database-filter");
    expect(structureIframe(view.container)).toBe(firstIframe);

    fireEvent.click(screen.getByRole("button", { name: "结构工作台" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "结构工作台" })).not.toBeNull());
    expect(structureIframe(view.container)).toBe(firstIframe);
  });

  it("进入其它 Ketcher owner 时卸载工作台，返回后只挂载一个新实例", async () => {
    const view = render(<App />);
    const firstIframe = structureIframe(view.container);
    expect(firstIframe).not.toBeNull();

    openDiscoverGroup();
    fireEvent.click(screen.getByRole("button", { name: "数据库查询" }));
    await screen.findByTestId("database-query");
    expect(structureIframe(view.container)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "结构工作台" }));
    await waitFor(() => expect(structureIframe(view.container)).not.toBeNull());
    expect(structureIframe(view.container)).not.toBe(firstIframe);
    expect(view.container.querySelectorAll('iframe[title="结构工作台结构编辑器"]')).toHaveLength(1);
  });

  it("均聚物预测深链和工作台跳转始终只挂载一个共享 Ketcher", async () => {
    const view = render(<App />);
    openBuildGroup();
    fireEvent.click(screen.getByRole("button", { name: "均聚物性质预测" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "均聚物性质预测" })).toBeTruthy());
    expect(structureIframe(view.container)).toBeNull();
    expect(view.container.querySelectorAll('iframe[src="/ketcher/index.html"]')).toHaveLength(1);
    expect(screen.getByTitle("均聚物性质预测结构编辑器")).toBeTruthy();
    expect(window.location.pathname).toBe("/homopolymer-property-prediction");

    view.unmount();
    window.history.replaceState({}, "", "/homopolymer-property-prediction");
    const deepLink = render(<App />);
    expect(await screen.findByRole("heading", { name: "均聚物性质预测" })).toBeTruthy();
    expect(deepLink.container.querySelectorAll('iframe[src="/ketcher/index.html"]')).toHaveLength(1);
    expect(screen.getByRole("button", { name: "均聚物性质预测" }).getAttribute("aria-current")).toBe("page");
  });

  it("相似性探索深链使用单一共享 Ketcher，并在离页前同步画板", async () => {
    const deferred: { resolve?: () => void } = {};
    mocks.syncSmilesFromCanvas.mockReturnValue(new Promise<string>((resolve) => {
      deferred.resolve = () => resolve("*CC*");
    }));
    window.history.replaceState({}, "", "/explorer");
    const view = render(<App />);

    expect(await screen.findByRole("heading", { name: "聚合物相似性探索" })).toBeTruthy();
    expect(view.container.querySelectorAll('iframe[src="/ketcher/index.html"]')).toHaveLength(1);
    expect(screen.getByTitle("聚合物相似性探索结构编辑器")).toBeTruthy();
    expect(screen.getByRole("button", { name: "聚合物相似性探索" }).getAttribute("aria-current")).toBe("page");

    window.history.pushState({}, "", "/knowledge");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.queryByTestId("knowledge")).toBeNull();
    await waitFor(() => expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1));

    deferred.resolve?.();
    await screen.findByTestId("knowledge");
  });

  it("侧栏导航等待同步并对重复激活只执行一次目标跳转", async () => {
    const deferred: { resolve?: () => void } = {};
    mocks.syncSmilesFromCanvas.mockReturnValue(new Promise<string>((resolve) => {
      deferred.resolve = () => resolve("");
    }));
    const pushState = vi.spyOn(window.history, "pushState");
    render(<App />);

    openDiscoverGroup();
    const target = screen.getByRole("button", { name: "数据库筛选" });
    fireEvent.click(target);
    fireEvent.click(target);
    expect(screen.queryByTestId("database-filter")).toBeNull();
    await waitFor(() => expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1));

    deferred.resolve?.();
    await screen.findByTestId("database-filter");
    expect(pushState.mock.calls.filter(([, , url]) => url === "/database-filter")).toHaveLength(1);
  });

  it("连续 popstate 共用同步任务并只应用最新目标", async () => {
    const deferred: { resolve?: () => void } = {};
    mocks.syncSmilesFromCanvas.mockReturnValue(new Promise<string>((resolve) => {
      deferred.resolve = () => resolve("");
    }));
    render(<App />);

    window.history.pushState({}, "", "/database-filter");
    fireEvent(window, new PopStateEvent("popstate"));
    window.history.pushState({}, "", "/knowledge");
    fireEvent(window, new PopStateEvent("popstate"));
    await waitFor(() => expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1));

    deferred.resolve?.();
    await screen.findByTestId("knowledge");
    expect(screen.queryByTestId("database-filter")).toBeNull();
  });

  it("均聚物预测页的 popstate 同样等待共享画板同步", async () => {
    const deferred: { resolve?: () => void } = {};
    mocks.syncSmilesFromCanvas.mockReturnValue(new Promise<string>((resolve) => {
      deferred.resolve = () => resolve("*CC*");
    }));
    window.history.replaceState({}, "", "/homopolymer-property-prediction");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "均聚物性质预测" })).toBeTruthy();

    window.history.pushState({}, "", "/knowledge");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.queryByTestId("knowledge")).toBeNull();
    await waitFor(() => expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1));

    deferred.resolve?.();
    await screen.findByTestId("knowledge");
  });

  it("等待中的侧栏目标会被更新的 popstate 目标取消", async () => {
    const deferred: { resolve?: () => void } = {};
    mocks.syncSmilesFromCanvas.mockReturnValue(new Promise<string>((resolve) => {
      deferred.resolve = () => resolve("");
    }));
    render(<App />);

    openDiscoverGroup();
    fireEvent.click(screen.getByRole("button", { name: "数据库筛选" }));
    window.history.pushState({}, "", "/knowledge");
    fireEvent(window, new PopStateEvent("popstate"));
    deferred.resolve?.();

    await screen.findByTestId("knowledge");
    expect(screen.queryByTestId("database-filter")).toBeNull();
    expect(window.location.pathname).toBe("/knowledge");
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1);
  });
});
