/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

type MockDatasetKey = "process" | "property" | "structureEffect" | "dft" | "formulation";

vi.mock("./components/DatabaseAnalysis", () => ({
  DatabaseAnalysis: ({
    selectedKey,
    onOpenDataset,
    onBackDatabase
  }: {
    selectedKey: MockDatasetKey | null;
    onOpenDataset: (key: MockDatasetKey) => void;
    onBackDatabase: () => void;
  }) => (
    <div data-testid="database-analysis-route">
      <span>{selectedKey ?? "overview"}</span>
      <button type="button" onClick={() => onOpenDataset("property")}>打开实验性能</button>
      <button type="button" onClick={onBackDatabase}>返回全库概览</button>
    </div>
  )
}));

vi.mock("./components/AgentWorkspaceHomePage", () => ({
  AgentWorkspaceHomePage: () => null,
  agentWorkspaceUrl: () => null
}));

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("数据库分析深链路由", () => {
  it.each([
    ["/database", "overview"],
    ["/database/process", "process"],
    ["/database/property", "property"],
    ["/database/structure-effect", "structureEffect"],
    ["/database/dft", "dft"],
    ["/database/formulation", "formulation"]
  ])("直接访问 %s 恢复对应工作面", (path, expectedView) => {
    window.history.replaceState({}, "", path);
    render(<App />);

    expect(screen.getByTestId("database-analysis-route").textContent).toContain(expectedView);
    expect(screen.getByRole("button", { name: "数据库分析" }).getAttribute("aria-current")).toBe("page");
  });

  it("数据集回调与 popstate 均保持深链和工作面同步", () => {
    window.history.replaceState({}, "", "/database");
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "打开实验性能" }));
    expect(window.location.pathname).toBe("/database/property");
    expect(screen.getByTestId("database-analysis-route").textContent).toContain("property");

    window.history.pushState({}, "", "/database/formulation");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.getByTestId("database-analysis-route").textContent).toContain("formulation");

    fireEvent.click(screen.getByRole("button", { name: "返回全库概览" }));
    expect(window.location.pathname).toBe("/database");
    expect(screen.getByTestId("database-analysis-route").textContent).toContain("overview");
  });
});
