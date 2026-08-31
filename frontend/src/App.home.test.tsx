/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

vi.mock("./components/DatabaseFilterPage", () => ({
  DatabaseFilterPage: () => <div data-testid="database-filter-page">数据库筛选页面</div>
}));

const WORKSPACE_ORIGIN = "http://workspace.example.test:9011";
const WORKSPACE_URL = `${WORKSPACE_ORIGIN}/`;

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  vi.stubEnv("VITE_AGENT_WORKSPACE_URL", WORKSPACE_URL);
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("智聚万物首页", () => {
  it("侧栏按研发阶段组织模块，并将结构工作台独立置顶", () => {
    render(<App />);

    const navigation = screen.getByRole("navigation", { name: "业务模块" });
    const structureWorkbench = screen.getByRole("button", { name: "结构工作台" });
    const groupTitles = Array.from(
      navigation.querySelectorAll<HTMLButtonElement>("section > button")
    ).map((button) => button.getAttribute("aria-label"));

    expect(structureWorkbench.closest("section")).toBeNull();
    expect(groupTitles).toEqual([
      "材料发现 Discover",
      "材料设计 Build",
      "实验优化 Optimize",
      "数据管理 Data"
    ]);

    const expectedModules = new Map([
      [
        "材料发现 Discover",
        ["知识检索", "聚合物生成", "聚合物相似性探索", "数据库查询", "数据库筛选", "数据库分析"]
      ],
      [
        "材料设计 Build",
        ["均聚物性质预测", "单体正向聚合", "MD 模拟", "单体 MD 模拟", "单体 DFT（AIMNet2）", "条件聚合物生成"]
      ],
      ["实验优化 Optimize", ["Tg 逆向设计", "高通量优化演示"]]
    ]);

    expectedModules.forEach((labels, title) => {
      const toggle = screen.getByRole("button", { name: title });
      fireEvent.click(toggle);
      expect(
        Array.from(
          toggle.closest("section")?.querySelectorAll<HTMLElement>("[data-module-id]") ?? []
        ).map((item) => item.textContent?.trim())
      ).toEqual(labels);
    });

    const dataGroup = screen.getByRole("button", { name: "数据管理 Data" }) as HTMLButtonElement;
    expect(dataGroup.disabled).toBe(true);
    expect(dataGroup.closest("section")?.querySelectorAll("[data-module-id]")).toHaveLength(0);
  });

  it("支持数据库筛选规范路由，并将旧地址历史替换到新路由", () => {
    window.history.replaceState({}, "", "/database-filter");
    const canonical = render(<App />);

    expect(screen.getByTestId("database-filter-page")).not.toBeNull();
    expect(screen.getByRole("button", { name: "数据库筛选" }).getAttribute("aria-current")).toBe("page");

    window.history.pushState({}, "", "/database");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.queryByTestId("database-filter-page")).toBeNull();
    window.history.pushState({}, "", "/database-filter");
    fireEvent(window, new PopStateEvent("popstate"));
    expect(screen.getByTestId("database-filter-page")).not.toBeNull();
    canonical.unmount();

    window.history.replaceState({}, "", "/database/property-filter");
    render(<App />);
    expect(window.location.pathname).toBe("/database-filter");
    expect(screen.getByTestId("database-filter-page")).not.toBeNull();
  });

  it("将旧知识检索重复 term 深链规范化为 AND 分组", async () => {
    window.history.replaceState({}, "", "/knowledge?q=epoxy+OR+NMP&term=epoxy&term=NMP");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      query: "epoxy；NMP",
      groups: [{ terms: ["epoxy"] }, { terms: ["NMP"] }],
      terms: ["epoxy", "NMP"],
      page: 1,
      page_size: 20,
      query_time_ms: 1,
      total: 0,
      results: []
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    render(<App />);

    expect((screen.getByRole("searchbox", { name: "本地知识库检索词" }) as HTMLInputElement).value).toBe("epoxy；NMP");
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("q")).toBe("epoxy；NMP"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const request = fetchMock.mock.calls.find(([url]) => String(url).includes("/knowledge/search"));
    expect(request).toBeDefined();
    expect(JSON.parse(String(request?.[1]?.body))).toEqual(expect.objectContaining({
      query: "epoxy；NMP",
      groups: [{ terms: ["epoxy"] }, { terms: ["NMP"] }]
    }));
  });

  it("未配置工作台时只显示同步占位且不挂载 iframe", () => {
    // unstubAllEnvs restores the process value, which may be configured in dev.
    vi.stubEnv("VITE_AGENT_WORKSPACE_URL", undefined);
    render(<App />);

    expect(screen.queryByText("今天研究什么聚合物问题？")).toBeNull();
    expect(screen.getByText("正在同步", { selector: '[role="status"]' })).not.toBeNull();
    expect(screen.queryByTitle("智聚万物智能体工作台")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));
    expect((screen.getByRole("button", { name: "搜索项目" }) as HTMLButtonElement).disabled).toBe(
      true
    );
    expect((screen.getByRole("button", { name: "新建项目" }) as HTMLButtonElement).disabled).toBe(
      true
    );
    expect(screen.getByText("正在同步项目…")).not.toBeNull();
    expect(screen.getByText("正在同步会话…")).not.toBeNull();
  });

  it.each(["   ", "file:///tmp/workspace", "https://user:secret@workspace.example.test"])(
    "工作台配置 %s 无效时保持同步占位",
    (workspaceUrl) => {
      vi.stubEnv("VITE_AGENT_WORKSPACE_URL", workspaceUrl);
      render(<App />);

      expect(screen.getByText("正在同步", { selector: '[role="status"]' })).not.toBeNull();
      expect(screen.queryByTitle("智聚万物智能体工作台")).toBeNull();
    }
  );

  it("使用部署环境配置的 9011 智能体工作台地址", () => {
    render(<App />);

    expect(screen.getByTitle("智聚万物智能体工作台").getAttribute("src")).toBe(WORKSPACE_URL);
  });

  it("使用部署环境配置的智能体工作台地址", () => {
    vi.stubEnv("VITE_AGENT_WORKSPACE_URL", "https://workspace.example.test");

    render(<App />);

    expect(screen.getByTitle("智聚万物智能体工作台").getAttribute("src")).toBe(
      "https://workspace.example.test/"
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
      WORKSPACE_ORIGIN
    );
    expect(postMessage).toHaveBeenCalledWith(
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "general.sessions.request"
      },
      WORKSPACE_ORIGIN
    );

    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: WORKSPACE_ORIGIN,
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

    fireEvent.click(await waitFor(() => screen.getByRole("button", { name: "展开项目" })));
    const projectButton = await waitFor(() => {
      const button = screen
        .getAllByRole("button")
        .find(
          (candidate) =>
            candidate.getAttribute("data-project-directory") === "/home/codexlab/DevTool/Alpha"
        );
      if (!button) {
        throw new Error("尚未渲染 Alpha 项目按钮");
      }
      return button;
    });
    fireEvent.click(projectButton);

    expect(postMessage).toHaveBeenCalledWith(
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.open",
        directory: "/home/codexlab/DevTool/Alpha"
      },
      WORKSPACE_ORIGIN
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
        origin: WORKSPACE_ORIGIN,
        data: {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "projects.snapshot",
          projects: [],
          activeDirectory: null
        }
      })
    );

    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));
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
      WORKSPACE_ORIGIN
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      2,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.new"
      },
      WORKSPACE_ORIGIN
    );
  });

  it("从项目菜单发送收藏目标状态和归档命令", async () => {
    render(<App />);

    const workspace = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    const frameWindow = workspace.contentWindow;
    expect(frameWindow).not.toBeNull();
    const postMessage = vi.spyOn(frameWindow!, "postMessage").mockImplementation(() => {});

    fireEvent.load(workspace);
    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: WORKSPACE_ORIGIN,
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
          activeDirectory: "/home/codexlab/DevTool/Alpha"
        }
      })
    );

    const menuButton = await waitFor(() => screen.getByRole("button", { name: "打开 Alpha 项目菜单" }));
    postMessage.mockClear();

    fireEvent.click(menuButton);
    fireEvent.click(screen.getByRole("menuitem", { name: "取消收藏" }));
    fireEvent.click(menuButton);
    fireEvent.click(screen.getByRole("menuitem", { name: "归档项目" }));

    expect(postMessage).toHaveBeenNthCalledWith(
      1,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.favorite.set",
        directory: "/home/codexlab/DevTool/Alpha",
        favorite: false
      },
      WORKSPACE_ORIGIN
    );
    expect(postMessage).toHaveBeenNthCalledWith(
      2,
      {
        namespace: "openscience.zhijupoly",
        version: 1,
        type: "project.archive",
        directory: "/home/codexlab/DevTool/Alpha"
      },
      WORKSPACE_ORIGIN
    );
  });

  it("接收通用会话快照并发送新建、打开、重命名和删除命令", async () => {
    render(<App />);

    const workspace = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    const frameWindow = workspace.contentWindow;
    expect(frameWindow).not.toBeNull();
    const postMessage = vi.spyOn(frameWindow!, "postMessage").mockImplementation(() => {});

    fireEvent.load(workspace);
    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: WORKSPACE_ORIGIN,
        data: {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.sessions.snapshot",
          sessions: [
            { id: "ses_new", title: "新的研究对话", updatedAt: 200 },
            { id: "ses_old", title: "旧的实验记录", updatedAt: 100 }
          ],
          activeSessionID: "ses_new"
        }
      })
    );

    await waitFor(() => screen.getByRole("button", { name: "新的研究对话" }));
    postMessage.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "新建对话" }));
    fireEvent.click(screen.getByRole("button", { name: "旧的实验记录" }));
    fireEvent.doubleClick(screen.getByRole("button", { name: "新的研究对话" }));
    const renameInput = screen.getByRole("textbox", { name: "重命名会话" });
    fireEvent.change(renameInput, { target: { value: "更新后的标题" } });
    fireEvent.keyDown(renameInput, { key: "Enter" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "删除会话 旧的实验记录" }));

    expect(postMessage.mock.calls).toEqual([
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.new"
        },
        WORKSPACE_ORIGIN
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.open",
          sessionID: "ses_old"
        },
        WORKSPACE_ORIGIN
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.rename",
          sessionID: "ses_new",
          title: "更新后的标题"
        },
        WORKSPACE_ORIGIN
      ],
      [
        {
          namespace: "openscience.zhijupoly",
          version: 1,
          type: "general.session.delete",
          sessionID: "ses_old"
        },
        WORKSPACE_ORIGIN
      ]
    ]);
  });

  it("打开普通项目后只保留通用对话入口，点击入口重新加载根通用页面", async () => {
    render(<App />);

    const workspace = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    const frameWindow = workspace.contentWindow;
    expect(frameWindow).not.toBeNull();
    const postMessage = vi.spyOn(frameWindow!, "postMessage").mockImplementation(() => {});

    fireEvent.load(workspace);
    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: WORKSPACE_ORIGIN,
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
              favorite: false
            }
          ],
          activeDirectory: null
        }
      })
    );

    fireEvent.click(await waitFor(() => screen.getByRole("button", { name: "展开项目" })));
    const projectButton = await waitFor(() => {
      const button = screen
        .getAllByRole("button")
        .find((candidate) => candidate.getAttribute("data-project-directory") === "/home/codexlab/DevTool/Alpha");
      if (!button) {
        throw new Error("尚未渲染 Alpha 项目按钮");
      }
      return button;
    });
    fireEvent.click(projectButton);

    expect(screen.queryByRole("button", { name: "新建对话" })).toBeNull();
    expect(screen.getByRole("button", { name: "对话" })).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "对话" }));
    expect(workspace.src).toBe(WORKSPACE_URL);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "展开项目" }).getAttribute("aria-expanded")).toBe("false");
    });
    expect(screen.queryByRole("button", { name: "搜索项目" })).toBeNull();
    expect(
      screen
        .queryAllByRole("button")
      .find((candidate) => candidate.getAttribute("data-project-directory") === "/home/codexlab/DevTool/Alpha")
    ).toBeUndefined();
  });

  it("离开首页后清除工作台活动态并允许对话入口返回首页", () => {
    render(<App />);

    const conversationEntry = screen.getByRole("button", { name: "对话" });
    expect(conversationEntry.getAttribute("aria-current")).toBe("page");

    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
    fireEvent.click(screen.getByRole("button", { name: "知识检索" }));

    expect(conversationEntry.getAttribute("aria-current")).toBeNull();
    fireEvent.click(conversationEntry);
    expect(conversationEntry.getAttribute("aria-current")).toBe("page");
  });

  it("离开首页后不再把 OpenScience 项目标记为当前页面", async () => {
    render(<App />);

    const workspace = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    const frameWindow = workspace.contentWindow;
    expect(frameWindow).not.toBeNull();

    window.dispatchEvent(
      new MessageEvent("message", {
        source: frameWindow,
        origin: WORKSPACE_ORIGIN,
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
          activeDirectory: "/home/codexlab/DevTool/Alpha"
        }
      })
    );

    await waitFor(() => {
      const activeProject = screen
        .getAllByRole("button")
        .find(
          (candidate) =>
            candidate.getAttribute("data-project-directory") === "/home/codexlab/DevTool/Alpha"
        );
      expect(activeProject?.getAttribute("aria-current")).toBe("page");
    });

    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
    fireEvent.click(screen.getByRole("button", { name: "知识检索" }));
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));

    const inactiveProject = screen
      .getAllByRole("button")
      .find(
        (candidate) =>
          candidate.getAttribute("data-project-directory") === "/home/codexlab/DevTool/Alpha"
      );
    expect(inactiveProject?.getAttribute("aria-current")).toBeNull();
  });
});
