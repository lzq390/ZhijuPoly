// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { preloadPage } from "./pages";
import { pathFromRoute, routeFromPath, type ActiveModule } from "./routing";

vi.mock("./components/OverviewPage", () => ({
  OverviewPage: ({ onNavigate }: { onNavigate: (module: ActiveModule) => void }) => (
    <section>
      <h1>总览内容</h1>
      <button onClick={() => onNavigate("knowledge")}>从总览打开知识检索</button>
    </section>
  )
}));
vi.mock("./components/KnowledgeSearch", () => ({
  KnowledgeSearch: () => <section><h1>知识检索内容</h1></section>
}));

beforeAll(async () => {
  await Promise.all([preloadPage("overview"), preloadPage("knowledge")]);
});

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  vi.stubEnv("VITE_AGENT_WORKSPACE_URL", "http://workspace.example.test:9011/");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("系统总览路由集成", () => {
  it("提供独立的总览双向路由并保留原 Agent 首页路由", () => {
    expect(routeFromPath("/overview/")).toEqual({ module: "overview", datasetKey: null });
    expect(pathFromRoute({ module: "overview", datasetKey: null })).toBe("/overview");
    expect(routeFromPath("/")).toEqual({ module: "home", datasetKey: null });
    expect(pathFromRoute({ module: "home", datasetKey: null })).toBe("/");
  });

  it("从侧栏进入总览并支持快捷入口、历史导航和返回原 Agent 首页", async () => {
    render(<App />);
    const workspace = screen.getByTitle("智聚万物智能体工作台");
    const conversation = screen.getByRole("button", { name: "对话" });
    const overview = screen.getByRole("button", { name: "系统总览" });
    expect(conversation.getAttribute("aria-current")).toBe("page");

    fireEvent.click(overview);
    expect(await screen.findByRole("heading", { name: "总览内容" })).toBeTruthy();
    expect(window.location.pathname).toBe("/overview");
    expect(overview.getAttribute("aria-current")).toBe("page");
    expect(conversation.getAttribute("aria-current")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "从总览打开知识检索" }));
    expect(await screen.findByRole("heading", { name: "知识检索内容" })).toBeTruthy();
    expect(window.location.pathname).toBe("/knowledge");
    expect(overview.getAttribute("aria-current")).toBeNull();

    window.history.replaceState({}, "", "/overview");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(await screen.findByRole("heading", { name: "总览内容" })).toBeTruthy();
    expect(overview.getAttribute("aria-current")).toBe("page");
    expect(screen.getByTitle("智聚万物智能体工作台")).toBe(workspace);

    fireEvent.click(conversation);
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(conversation.getAttribute("aria-current")).toBe("page");
    expect(screen.getByTitle("智聚万物智能体工作台").getAttribute("src")).toBe("http://workspace.example.test:9011/");
  });

  it("通过总览深链加载满高页面，让总览管理自己的页头和滚动区域", () => {
    window.history.replaceState({}, "", "/overview");
    const view = render(<App />);
    expect(screen.getByRole("heading", { name: "总览内容" })).toBeTruthy();
    expect(screen.getByRole("main").className).toContain("p-0");
    expect(screen.getByRole("main").className).toContain("overflow-hidden");
    const content = view.container.querySelector<HTMLElement>('[data-module-content="overview"]');
    expect(content?.className).toContain("h-full min-h-0 gap-0");
    expect(content?.className).toContain("max-w-none");
  });
});
