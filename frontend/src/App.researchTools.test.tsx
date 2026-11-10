/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { preloadPage } from "./pages";
import type { ActiveModule } from "./routing";

const modules = [
  ["knowledge", "知识检索", "/knowledge"],
  ["polytaoGeneration", "聚合物生成", "/polytao-generation"],
  ["monomerRetrosynthesis", "单体逆合成反推", "/monomer-retrosynthesis"],
  ["explorer", "聚合物相似性探索", "/explorer"],
  ["databaseQuery", "数据库查询", "/database-query"],
  ["databaseFilter", "数据库筛选", "/database-filter"],
  ["database", "数据库分析", "/database"],
  ["homopolymerPrediction", "均聚物性质预测", "/homopolymer-property-prediction"],
  ["monomerPolymerization", "单体正向聚合", "/monomer-polymerization"],
  ["mdSimulationDemo", "MD 模拟", "/md-simulation"],
  ["monomerMdSimulation", "单体 MD 模拟", "/monomer-md-simulation"],
  ["monomerDft", "单体 DFT", "/monomer-dft"],
  ["conditionalGeneration", "条件聚合物生成", "/conditional-generation"],
  ["reverseDesign", "Tg 逆向设计", "/reverse-design"],
  ["highThroughputWorkflowDemo", "高通量优化演示", "/high-throughput-workflow-demo"]
] as const;

const mocks = vi.hoisted(() => ({ rendered: vi.fn() }));
function ModuleFixture({ module }: { module: string }) {
  mocks.rendered(module);
  return <div data-testid="module-fixture">{module}</div>;
}
vi.mock("./components/KnowledgeSearch", () => ({ KnowledgeSearch: () => <ModuleFixture module="knowledge" /> }));
vi.mock("./components/PolytaoGenerationPage", () => ({ PolytaoGenerationPage: () => <ModuleFixture module="polytaoGeneration" /> }));
vi.mock("./components/MonomerRetrosynthesisPage", () => ({ MonomerRetrosynthesisPage: () => <ModuleFixture module="monomerRetrosynthesis" /> }));
vi.mock("./components/PolymerSimilarityExplorerPage", () => ({ PolymerSimilarityExplorerPage: () => <ModuleFixture module="explorer" /> }));
vi.mock("./components/DatabaseQueryPage", () => ({ DatabaseQueryPage: () => <ModuleFixture module="databaseQuery" /> }));
vi.mock("./components/DatabaseFilterPage", () => ({ DatabaseFilterPage: () => <ModuleFixture module="databaseFilter" /> }));
vi.mock("./components/DatabaseAnalysis", () => ({ DatabaseAnalysis: () => <ModuleFixture module="database" /> }));
vi.mock("./components/HomopolymerPropertyPredictionPage", () => ({ HomopolymerPropertyPredictionPage: () => <ModuleFixture module="homopolymerPrediction" /> }));
vi.mock("./components/MonomerPolymerizationPage", () => ({ MonomerPolymerizationPage: () => <ModuleFixture module="monomerPolymerization" /> }));
vi.mock("./components/PolymerMdSimulationPage", () => ({ PolymerMdSimulationPage: () => <ModuleFixture module="mdSimulationDemo" /> }));
vi.mock("./components/MonomerMdSimulationPage", () => ({ MonomerMdSimulationPage: () => <ModuleFixture module="monomerMdSimulation" /> }));
vi.mock("./components/MonomerDftPage", () => ({ MonomerDftPage: () => <ModuleFixture module="monomerDft" /> }));
vi.mock("./components/ConditionalGenerationPage", () => ({ ConditionalGenerationPage: () => <ModuleFixture module="conditionalGeneration" /> }));
vi.mock("./components/ReverseDesignPage", () => ({ ReverseDesignPage: () => <ModuleFixture module="reverseDesign" /> }));
vi.mock("./components/HighThroughputWorkflowDemoPage", () => ({ HighThroughputWorkflowDemoPage: () => <ModuleFixture module="highThroughputWorkflowDemo" /> }));

beforeAll(async () => {
  await Promise.all(["researchTools", ...modules.map(([module]) => module)].map(module => preloadPage(module as ActiveModule)));
});
beforeEach(() => {
  window.history.replaceState({}, "", "/research-tools");
  mocks.rendered.mockClear();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function expandAll() {
  screen.getAllByRole("button", { name: /^(材料发现|材料设计|实验优化).*展开/ }).forEach(button => fireEvent.click(button));
}

describe("实验工具目录接入", () => {
  it("直达目录不挂载计算模块或发送计算请求", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    render(<App />);
    expect(screen.getByRole("heading", { name: "实验工具" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "实验工具" }).getAttribute("aria-current")).toBe("page");
    expect(mocks.rendered).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    expect(document.querySelector("[data-structure-editor]")).toBeNull();
  });

  it.each(modules)("%s 卡片保持原路由、直达刷新与目录父级高亮", async (module, label, path) => {
    const app = render(<App />);
    expandAll();
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(screen.getByTestId("module-fixture").textContent).toBe(module));
    expect(window.location.pathname).toBe(path);
    expect(screen.getByRole("button", { name: "实验工具" }).getAttribute("aria-current")).toBe("page");
    app.unmount();
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("module-fixture").textContent).toBe(module));
    expect(window.location.pathname).toBe(path);
  });

  it("模块往返及浏览器后退恢复独立展开状态，刷新目录恢复默认单行", async () => {
    const app = render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /材料发现.*展开/ }));
    fireEvent.click(screen.getByRole("button", { name: /材料设计.*展开/ }));
    fireEvent.click(screen.getByRole("button", { name: "知识检索" }));
    await screen.findByTestId("module-fixture");
    window.history.back();
    await screen.findByRole("heading", { name: "实验工具" });
    expect(screen.getByRole("button", { name: "材料发现：仅显示一行" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "材料设计：仅显示一行" }).getAttribute("aria-expanded")).toBe("true");
    window.history.forward();
    await screen.findByTestId("module-fixture");
    fireEvent.click(screen.getByRole("button", { name: "实验工具" }));
    await screen.findByRole("heading", { name: "实验工具" });
    expect(screen.getByRole("button", { name: "材料发现：仅显示一行" })).toBeTruthy();
    app.unmount();
    render(<App />);
    expect(screen.getByRole("button", { name: /材料发现.*展开/ }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByRole("button", { name: /材料设计.*展开/ }).getAttribute("aria-expanded")).toBe("false");
  });

  it("保留大类完全折叠和显示更多的独立状态，刷新后恢复默认单行", async () => {
    const app = render(<App />);
    fireEvent.click(screen.getByRole("button", { name: /材料发现.*展开更多/ }));
    fireEvent.click(screen.getByRole("button", { name: "材料设计：收起" }));
    expect(document.querySelector('[data-research-group="build"] [data-module-id]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "知识检索" }));
    await screen.findByTestId("module-fixture");
    fireEvent.click(screen.getByRole("button", { name: "实验工具" }));
    await screen.findByRole("heading", { name: "实验工具" });
    expect(screen.getByRole("button", { name: "材料设计：展开" }).getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[data-research-group="build"] [data-module-id]')).toBeNull();
    expect(screen.getByRole("button", { name: "材料发现：仅显示一行" }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "材料设计：展开" }));
    expect(document.querySelector('[data-research-group="build"] [data-module-id]')).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "材料设计：收起" }));
    app.unmount();
    render(<App />);
    expect(document.querySelector('[data-research-group="build"] [data-module-id]')).toBeTruthy();
    expect(screen.getByRole("button", { name: /材料发现.*展开更多/ }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "材料设计：展开" })).toBeNull();
  });
});
