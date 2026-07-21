/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.unstubAllEnvs();
});

describe("智聚万物首页", () => {
  it("由修改后的智能体工作台接管旧问答首页", () => {
    render(<App />);

    expect(screen.queryByText("今天研究什么聚合物问题？")).toBeNull();

    const workspace = screen.queryByTitle("智聚万物智能体工作台");
    expect(workspace).not.toBeNull();
    expect(workspace?.getAttribute("src")).toBe("http://127.0.0.1:4454");
  });

  it("使用部署环境配置的智能体工作台地址", () => {
    vi.stubEnv("VITE_AGENT_WORKSPACE_URL", "https://workspace.example.test");

    render(<App />);

    expect(screen.getByTitle("智聚万物智能体工作台").getAttribute("src")).toBe(
      "https://workspace.example.test"
    );
  });
});
