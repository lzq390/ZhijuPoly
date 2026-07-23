/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
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

  it("接收工作台项目快照并从侧栏发送打开命令", async () => {
    render(<App />);

    const workspace = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    const frameWindow = workspace.contentWindow;
    expect(frameWindow).not.toBeNull();
    const postMessage = vi.spyOn(frameWindow!, "postMessage").mockImplementation(() => {});

    fireEvent.load(workspace);
    expect(postMessage).toHaveBeenCalledWith(
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "projects.request"
      },
      "http://127.0.0.1:4454"
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: "http://127.0.0.1:4454",
        data: {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "projects.snapshot",
          projects: [
            {
              directory: "/home/codexlab/DevTool/Alpha",
              name: "Alpha",
              displayPath: "~/DevTool/Alpha",
              updatedAt: 200,
              favorite: true
            }
          ],
          activeDirectory: null
        }
      })
    );

    const projectButton = await waitFor(() => screen.getByRole("button", { name: /Alpha/ }));
    fireEvent.click(projectButton);

    expect(postMessage).toHaveBeenCalledWith(
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.open",
        directory: "/home/codexlab/DevTool/Alpha"
      },
      "http://127.0.0.1:4454"
    );
  });

  it("从侧栏发送搜索项目和新建项目命令", async () => {
    render(<App />);

    const workspace = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    const frameWindow = workspace.contentWindow;
    expect(frameWindow).not.toBeNull();
    const postMessage = vi.spyOn(frameWindow!, "postMessage").mockImplementation(() => {});

    fireEvent.load(workspace);
    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: "http://127.0.0.1:4454",
        data: {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "projects.snapshot",
          projects: [],
          activeDirectory: null
        }
      })
    );

    const browseButton = await waitFor(() => screen.getByRole("button", { name: "搜索项目" }));
    expect((browseButton as HTMLButtonElement).disabled).toBe(false);
    postMessage.mockClear();

    fireEvent.click(browseButton);
    fireEvent.click(screen.getByRole("button", { name: "新建项目" }));

    expect(postMessage).toHaveBeenNthCalledWith(
      1,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "projects.browse"
      },
      "http://127.0.0.1:4454"
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      2,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.new"
      },
      "http://127.0.0.1:4454"
    );
  });
});
