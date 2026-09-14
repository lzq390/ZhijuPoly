// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { preloadPage } from "./pages";

// These cases exercise routing after transport; cold loading is covered separately.
beforeAll(async () => {
  await Promise.all(["monomerMdSimulation"].map(module => preloadPage(module as Parameters<typeof preloadPage>[0])));
});

vi.mock("./components/MonomerMdSimulationPage", () => ({
  MonomerMdSimulationPage: ({
    initialJobId,
    onJobIdChange
  }: {
    initialJobId: string | null;
    onJobIdChange: (jobId: string | null) => void;
  }) => (
    <div data-testid="monomer-md-route">
      <span>{initialJobId ?? "no-job"}</span>
      <button type="button" onClick={() => onJobIdChange("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")}>选择任务 B</button>
      <button type="button" onClick={() => onJobIdChange(null)}>清除任务</button>
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
  vi.restoreAllMocks();
});

describe("单体 MD 深链路由", () => {
  it("冷启动、任务回调、popstate 与清除查询参数保持同步", () => {
    const first = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const third = "cccccccccccccccccccccccccccccccc";
    window.history.replaceState({}, "", `/monomer-md-simulation?job=${first}`);
    render(<App />);

    expect(screen.getByTestId("monomer-md-route").textContent).toContain(first);
    fireEvent.click(screen.getByRole("button", { name: "选择任务 B" }));
    expect(window.location.pathname).toBe("/monomer-md-simulation");
    expect(window.location.search).toBe("?job=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(screen.getByTestId("monomer-md-route").textContent).toContain("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");

    window.history.pushState({}, "", `/monomer-md-simulation?job=${third}`);
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.getByTestId("monomer-md-route").textContent).toContain(third);

    fireEvent.click(screen.getByRole("button", { name: "清除任务" }));
    expect(window.location.search).toBe("");
    expect(screen.getByTestId("monomer-md-route").textContent).toContain("no-job");
  });

  it("非法查询不会成为可恢复任务 ID", () => {
    window.history.replaceState({}, "", "/monomer-md-simulation?job=../../secret");
    render(<App />);
    expect(screen.getByTestId("monomer-md-route").textContent).toContain("no-job");
  });
});
