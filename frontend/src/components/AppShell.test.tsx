/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AppShell,
  type AppShellModuleGroup,
  type AppShellModuleItem
} from "./AppShell";
import type { OpenScienceGeneralSessionSummary } from "../lib/openScienceGeneralSessionBridge";
import type { OpenScienceProjectSummary } from "../lib/openScienceProjectBridge";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function createStandaloneModules(activeId?: string): AppShellModuleItem[] {
  return [
    {
      id: "structureWorkbench",
      label: "结构工作台",
      description: "编辑共享结构",
      route: "/structure-workbench",
      icon: <span aria-hidden="true">S</span>,
      isActive: activeId === "structureWorkbench",
      onClick: vi.fn()
    }
  ];
}

function createModuleGroups(activeId?: string): AppShellModuleGroup[] {
  return [
    {
      id: "discover",
      label: "材料发现",
      secondaryLabel: "Discover",
      items: [
        {
          id: "knowledge",
          label: "知识检索",
          description: "检索知识",
          route: "/knowledge",
          icon: <span aria-hidden="true">K</span>,
          isActive: activeId === "knowledge",
          onClick: vi.fn()
        },
        {
          id: "databaseQuery",
          label: "数据库查询",
          description: "查询数据库",
          route: "/database-query",
          icon: <span aria-hidden="true">Q</span>,
          isActive: activeId === "databaseQuery",
          onClick: vi.fn()
        }
      ]
    },
    {
      id: "build",
      label: "材料设计",
      secondaryLabel: "Build",
      items: [
        {
          id: "monomerPolymerization",
          label: "单体正向聚合",
          description: "正向聚合",
          route: "/monomer-polymerization",
          icon: <span aria-hidden="true">B</span>,
          isActive: activeId === "monomerPolymerization",
          onClick: vi.fn()
        }
      ]
    },
    {
      id: "optimize",
      label: "实验优化",
      secondaryLabel: "Optimize",
      items: [
        {
          id: "reverseDesign",
          label: "Tg 逆向设计",
          description: "逆向设计",
          route: "/reverse-design",
          icon: <span aria-hidden="true">D</span>,
          isActive: activeId === "reverseDesign",
          onClick: vi.fn()
        }
      ]
    },
    {
      id: "data",
      label: "数据管理",
      secondaryLabel: "Data",
      items: [],
      emptyLabel: "暂无模块"
    }
  ];
}

const projects: OpenScienceProjectSummary[] = [
  {
    directory: "/home/codexlab/DevTool/Alpha",
    name: "Alpha",
    displayPath: "~/DevTool/Alpha",
    updatedAt: 200,
    favorite: true
  },
  {
    directory: "/home/codexlab/DevTool/Beta",
    name: "Beta",
    displayPath: "~/DevTool/Beta",
    updatedAt: 100,
    favorite: false
  }
];

const generalSessions: OpenScienceGeneralSessionSummary[] = [
  { id: "ses_new", title: "新的研究对话", updatedAt: 200 },
  { id: "ses_old", title: "旧的实验记录", updatedAt: 100 }
];

function renderShell(
  activeModule = "home",
  options?: {
    standaloneModules?: AppShellModuleItem[];
    moduleGroups?: AppShellModuleGroup[];
    onOpenHome?: () => void;
    projects?: OpenScienceProjectSummary[];
    activeDirectory?: string | null;
    isProjectBridgeReady?: boolean;
    onOpenProject?: (directory: string) => void;
    onBrowseProjects?: () => void;
    onNewProject?: () => void;
    onSetProjectFavorite?: (directory: string, favorite: boolean) => void;
    onArchiveProject?: (directory: string) => void;
    isGeneralWorkspaceActive?: boolean;
    generalSessions?: OpenScienceGeneralSessionSummary[];
    activeGeneralSessionID?: string | null;
    isGeneralSessionBridgeReady?: boolean;
    onOpenGeneralWorkspace?: () => void;
    onNewGeneralSession?: () => void;
    onOpenGeneralSession?: (sessionID: string) => void;
    onRenameGeneralSession?: (sessionID: string, title: string) => void;
    onDeleteGeneralSession?: (sessionID: string) => void;
    beforeNavigate?: () => Promise<void | boolean>;
  }
) {
  return render(
    <AppShell
      activeModule={activeModule}
      standaloneModules={options?.standaloneModules ?? createStandaloneModules(activeModule)}
      moduleGroups={options?.moduleGroups ?? createModuleGroups(activeModule)}
      onOpenHome={options?.onOpenHome ?? vi.fn()}
      projects={options?.projects ?? projects}
      activeProjectDirectory={options?.activeDirectory ?? null}
      isProjectBridgeReady={options?.isProjectBridgeReady ?? true}
      onOpenProject={options?.onOpenProject ?? vi.fn()}
      onBrowseProjects={options?.onBrowseProjects ?? vi.fn()}
      onNewProject={options?.onNewProject ?? vi.fn()}
      onSetProjectFavorite={options?.onSetProjectFavorite ?? vi.fn()}
      onArchiveProject={options?.onArchiveProject ?? vi.fn()}
      isGeneralWorkspaceActive={options?.isGeneralWorkspaceActive ?? !options?.activeDirectory}
      generalSessions={options?.generalSessions ?? generalSessions}
      activeGeneralSessionID={options?.activeGeneralSessionID ?? "ses_new"}
      isGeneralSessionBridgeReady={options?.isGeneralSessionBridgeReady ?? true}
      onOpenGeneralWorkspace={options?.onOpenGeneralWorkspace ?? vi.fn()}
      onNewGeneralSession={options?.onNewGeneralSession ?? vi.fn()}
      onOpenGeneralSession={options?.onOpenGeneralSession ?? vi.fn()}
      onRenameGeneralSession={options?.onRenameGeneralSession ?? vi.fn()}
      onDeleteGeneralSession={options?.onDeleteGeneralSession ?? vi.fn()}
      beforeNavigate={options?.beforeNavigate}
    >
      <div>页面内容</div>
    </AppShell>
  );
}

function getProjectButton(directory: string): HTMLButtonElement {
  const button = screen
    .getAllByRole("button")
    .find((candidate) => candidate.getAttribute("data-project-directory") === directory);
  if (!button) {
    throw new Error(`未找到项目按钮：${directory}`);
  }
  return button as HTMLButtonElement;
}

describe("AppShell 侧边栏", () => {
  it("聚合物相似性探索使用无内边距的满高工作台容器", () => {
    const view = renderShell("explorer");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("px-4")).toBe(false);
    expect(main?.firstElementChild?.classList.contains("h-full")).toBe(true);
  });

  it("均聚物性质预测使用无内边距的满高工作台容器", () => {
    const view = renderShell("homopolymerPrediction");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("px-4")).toBe(false);
    expect(main?.firstElementChild?.classList.contains("h-full")).toBe(true);
  });

  it("聚合物生成使用无内边距的满高工作台容器", () => {
    const view = renderShell("polytaoGeneration");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("px-4")).toBe(false);
    expect(main?.firstElementChild?.classList.contains("h-full")).toBe(true);
  });

  it("条件生成使用与 Tg 逆向设计一致的无内边距满高工作台容器", () => {
    const view = renderShell("conditionalGeneration");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("px-4")).toBe(false);
    expect(main?.firstElementChild?.classList.contains("h-full")).toBe(true);
  });

  it("数据库筛选使用无内边距的满高工作台容器", () => {
    const view = renderShell("databaseFilter");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("overflow-y-auto")).toBe(false);
  });

  it("数据库分析使用无内边距的满高工作台容器", () => {
    const view = renderShell("database");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("overflow-y-auto")).toBe(false);
  });

  it("知识检索使用无内边距的满高工作台容器", () => {
    const view = renderShell("knowledge");
    const main = view.container.querySelector("main");

    expect(main?.classList.contains("overflow-hidden")).toBe(true);
    expect(main?.classList.contains("p-0")).toBe(true);
    expect(main?.classList.contains("overflow-y-auto")).toBe(false);
  });

  it("只保留顶部品牌入口，不再渲染重复的智聚万物按钮", () => {
    renderShell();

    expect(screen.getAllByRole("button", { name: "智聚万物" })).toHaveLength(1);
  });

  it("结构工作台独立置顶，四个业务分类按研发流程排列", () => {
    renderShell();

    const navigation = screen.getByRole("navigation", { name: "业务模块" });
    const structureWorkbench = screen.getByRole("button", {
      name: "结构工作台"
    }) as HTMLButtonElement;
    const groups: HTMLButtonElement[] = [
      screen.getByRole("button", { name: "材料发现 Discover" }) as HTMLButtonElement,
      screen.getByRole("button", { name: "材料设计 Build" }) as HTMLButtonElement,
      screen.getByRole("button", { name: "实验优化 Optimize" }) as HTMLButtonElement,
      screen.getByRole("button", { name: "数据管理 Data" }) as HTMLButtonElement
    ];
    const navigationButtons = Array.from(navigation.querySelectorAll("button"));
    const groupIndices = groups.map((group) => navigationButtons.indexOf(group));

    expect(structureWorkbench.closest("section")).toBeNull();
    expect(navigationButtons.indexOf(structureWorkbench)).toBeLessThan(
      navigationButtons.indexOf(groups[0])
    );
    expect(groupIndices).toEqual([...groupIndices].sort((a, b) => a - b));

    const dataGroup = groups[3];
    expect(dataGroup.disabled).toBe(true);
    expect(dataGroup.getAttribute("aria-expanded")).toBe("false");
    expect(dataGroup.closest("section")?.querySelectorAll("[data-module-id]")).toHaveLength(0);
    expect(screen.getByText("暂无模块")).not.toBeNull();
  });

  it("业务模块组初始收起，并且每组可独立展开和收起", () => {
    renderShell();

    const discoverGroup = screen.getByRole("button", { name: "材料发现 Discover" });
    const optimizeGroup = screen.getByRole("button", { name: "实验优化 Optimize" });

    expect(discoverGroup.getAttribute("aria-expanded")).toBe("false");
    expect(optimizeGroup.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "数据库查询" })).toBeNull();

    fireEvent.click(discoverGroup);
    expect(discoverGroup.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "数据库查询" })).not.toBeNull();

    fireEvent.click(optimizeGroup);
    expect(discoverGroup.getAttribute("aria-expanded")).toBe("true");
    expect(optimizeGroup.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "数据库查询" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Tg 逆向设计" })).not.toBeNull();

    fireEvent.click(discoverGroup);
    expect(discoverGroup.getAttribute("aria-expanded")).toBe("false");
    expect(optimizeGroup.getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByRole("button", { name: "数据库查询" })).toBeNull();
    expect(screen.getByRole("button", { name: "Tg 逆向设计" })).not.toBeNull();

    fireEvent.click(optimizeGroup);
    expect(optimizeGroup.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "Tg 逆向设计" })).toBeNull();
  });

  it("进入业务模块时自动展开所属组并保留其他已展开组", () => {
    const view = renderShell();

    const optimizeGroup = screen.getByRole("button", { name: "实验优化 Optimize" });
    fireEvent.click(optimizeGroup);
    expect(optimizeGroup.getAttribute("aria-expanded")).toBe("true");

    view.rerender(
      <AppShell
        activeModule="knowledge"
        standaloneModules={createStandaloneModules("knowledge")}
        moduleGroups={createModuleGroups("knowledge")}
        onOpenHome={vi.fn()}
        projects={projects}
        activeProjectDirectory={null}
        isProjectBridgeReady
        onOpenProject={vi.fn()}
        onBrowseProjects={vi.fn()}
        onNewProject={vi.fn()}
        onSetProjectFavorite={vi.fn()}
        onArchiveProject={vi.fn()}
        isGeneralWorkspaceActive={false}
        generalSessions={generalSessions}
        activeGeneralSessionID={null}
        isGeneralSessionBridgeReady
        onOpenGeneralWorkspace={vi.fn()}
        onNewGeneralSession={vi.fn()}
        onOpenGeneralSession={vi.fn()}
        onRenameGeneralSession={vi.fn()}
        onDeleteGeneralSession={vi.fn()}
      >
        <div>页面内容</div>
      </AppShell>
    );

    expect(screen.getByRole("button", { name: "材料发现 Discover" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "实验优化 Optimize" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "知识检索" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Tg 逆向设计" })).not.toBeNull();
  });

  it("业务模块选中态使用与普通条目一致的扁平布局", () => {
    renderShell("structureWorkbench");
    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));

    const activeItem = screen.getByRole("button", { name: "结构工作台" });
    const inactiveItem = screen.getByRole("button", { name: "数据库查询" });

    expect(activeItem.getAttribute("aria-current")).toBe("page");
    expect(activeItem.getAttribute("data-active")).toBe("true");
    expect(activeItem.classList.contains("np-sidebar-module")).toBe(true);
    expect(inactiveItem.getAttribute("aria-current")).toBeNull();
    expect(inactiveItem.getAttribute("data-active")).toBe("false");
    expect(inactiveItem.classList.contains("np-sidebar-module")).toBe(true);
  });

  it("业务模块、项目和对话按内容高度顺序平铺并共用侧栏滚动", () => {
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
    fireEvent.click(screen.getByRole("button", { name: "实验优化 Optimize" }));
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));

    const moduleNavigation = screen.getByRole("navigation", { name: "业务模块" });
    const projectSection = screen.getByRole("heading", { name: "项目" }).closest("section");
    const generalSessionSection = screen.getByRole("heading", { name: "对话" }).closest("section");
    const scrollRegion = moduleNavigation.parentElement;
    const brandHeader = screen.getByRole("button", { name: "智聚万物" }).parentElement;

    if (!projectSection || !generalSessionSection || !scrollRegion || !brandHeader) {
      throw new Error("未找到完整的侧边栏内容流");
    }

    expect(brandHeader.parentElement).toBe(scrollRegion.parentElement);
    expect(brandHeader.classList.contains("np-sidebar__brand")).toBe(true);
    expect(scrollRegion.contains(brandHeader)).toBe(false);
    expect(projectSection.parentElement).toBe(scrollRegion);
    expect(generalSessionSection.parentElement).toBe(scrollRegion);

    const children = Array.from(scrollRegion.children);
    expect(children.indexOf(moduleNavigation)).toBeLessThan(children.indexOf(projectSection));
    expect(children.indexOf(projectSection)).toBeLessThan(children.indexOf(generalSessionSection));

    expect(scrollRegion.classList.contains("np-sidebar__scroll")).toBe(true);
    expect(scrollRegion.getAttribute("data-sidebar-scroll-region")).not.toBeNull();
    expect(scrollRegion.querySelector("[data-sidebar-scroll-region]")).toBeNull();
    expect(moduleNavigation.classList.contains("np-sidebar__modules")).toBe(true);
    expect(projectSection.classList.contains("np-sidebar-section")).toBe(true);
    expect(generalSessionSection.classList.contains("np-sidebar-section")).toBe(true);
  });

  it("项目区域默认折叠，展开后按快照顺序显示项目", () => {
    renderShell();

    expect(screen.getByRole("heading", { name: "项目" })).not.toBeNull();
    const toggle = screen.getByRole("button", { name: "展开项目" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Alpha")).toBeNull();

    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "收起项目" }).getAttribute("aria-expanded")).toBe("true");
    expect(getProjectButton("/home/codexlab/DevTool/Alpha")).not.toBeNull();
    expect(getProjectButton("/home/codexlab/DevTool/Beta")).not.toBeNull();

    const projectButtons = screen
      .getAllByRole("button")
      .filter((button) => button.getAttribute("data-project-directory"));
    expect(projectButtons.map((button) => button.getAttribute("data-project-directory"))).toEqual([
      "/home/codexlab/DevTool/Alpha",
      "/home/codexlab/DevTool/Beta"
    ]);
  });

  it("项目区域提供搜索和新建按钮并把操作交给上层", () => {
    const onBrowseProjects = vi.fn();
    const onNewProject = vi.fn();
    renderShell("home", { onBrowseProjects, onNewProject });

    fireEvent.click(screen.getByRole("button", { name: "新建项目" }));
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));
    fireEvent.click(screen.getByRole("button", { name: "搜索项目" }));

    expect(onBrowseProjects).toHaveBeenCalledTimes(1);
    expect(onNewProject).toHaveBeenCalledTimes(1);
  });

  it("高亮当前项目并把点击目录交给上层", () => {
    const onOpenProject = vi.fn();
    renderShell("home", {
      activeDirectory: "/home/codexlab/DevTool/Beta",
      onOpenProject
    });

    const activeProject = getProjectButton("/home/codexlab/DevTool/Beta");
    expect(activeProject.getAttribute("aria-current")).toBe("page");
    expect(screen.queryByText("Alpha")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));
    fireEvent.click(getProjectButton("/home/codexlab/DevTool/Alpha"));
    expect(onOpenProject).toHaveBeenCalledWith("/home/codexlab/DevTool/Alpha");
  });

  it("项目三点菜单发送目标收藏状态和归档操作且不会误触项目打开", () => {
    const onOpenProject = vi.fn();
    const onSetProjectFavorite = vi.fn();
    const onArchiveProject = vi.fn();
    renderShell("home", { onOpenProject, onSetProjectFavorite, onArchiveProject });
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));

    const alphaMenu = screen.getByRole("button", { name: "打开 Alpha 项目菜单" });
    expect(alphaMenu.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(alphaMenu);

    expect(alphaMenu.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("menu", { name: "Alpha 项目操作" })).not.toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "取消收藏" }));

    expect(onSetProjectFavorite).toHaveBeenCalledWith("/home/codexlab/DevTool/Alpha", false);
    expect(onOpenProject).not.toHaveBeenCalled();

    const betaMenu = screen.getByRole("button", { name: "打开 Beta 项目菜单" });
    fireEvent.click(betaMenu);
    fireEvent.click(screen.getByRole("menuitem", { name: "收藏项目" }));
    expect(onSetProjectFavorite).toHaveBeenCalledWith("/home/codexlab/DevTool/Beta", true);

    fireEvent.click(betaMenu);
    fireEvent.click(screen.getByRole("menuitem", { name: "归档项目" }));
    expect(onArchiveProject).toHaveBeenCalledWith("/home/codexlab/DevTool/Beta");
    expect(onOpenProject).not.toHaveBeenCalled();
  });

  it("项目菜单支持 Escape 和点击外部关闭并恢复触发按钮焦点", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));

    const trigger = screen.getByRole("button", { name: "打开 Alpha 项目菜单" });
    fireEvent.click(trigger);
    expect(screen.getByRole("menu", { name: "Alpha 项目操作" })).not.toBeNull();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Alpha 项目操作" })).toBeNull();
    expect(document.activeElement).toBe(trigger);

    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu", { name: "Alpha 项目操作" })).toBeNull();
  });

  it("桥接尚未就绪时显示低干扰加载状态", () => {
    renderShell("home", {
      projects: [],
      isProjectBridgeReady: false,
      generalSessions: [],
      isGeneralSessionBridgeReady: false
    });

    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));
    expect(screen.getByText("正在同步项目…")).not.toBeNull();
    expect((screen.getByRole("button", { name: "搜索项目" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "新建项目" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("正在同步会话…")).not.toBeNull();
    expect((screen.getByRole("button", { name: "新建对话" }) as HTMLButtonElement).disabled).toBe(
      true
    );
    expect(screen.queryByRole("button", { name: /打开 .* 项目菜单/ })).toBeNull();
  });

  it("通用模式显示新建、搜索、会话列表和活动态", () => {
    const onNewGeneralSession = vi.fn();
    const onOpenGeneralSession = vi.fn();
    renderShell("home", { onNewGeneralSession, onOpenGeneralSession });

    expect(screen.getByRole("heading", { name: "对话" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "新建对话" }));
    expect(onNewGeneralSession).toHaveBeenCalledTimes(1);

    const activeSession = screen.getByRole("button", { name: "新的研究对话" });
    expect(activeSession.getAttribute("aria-current")).toBe("page");
    fireEvent.click(screen.getByRole("button", { name: "旧的实验记录" }));
    expect(onOpenGeneralSession).toHaveBeenCalledWith("ses_old");

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索会话" }), {
      target: { value: "实验" }
    });
    expect(screen.queryByRole("button", { name: "新的研究对话" })).toBeNull();
    expect(screen.getByRole("button", { name: "旧的实验记录" })).not.toBeNull();
  });

  it("会话列表支持双击重命名并在确认后删除", () => {
    const onRenameGeneralSession = vi.fn();
    const onDeleteGeneralSession = vi.fn();
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    renderShell("home", { onRenameGeneralSession, onDeleteGeneralSession });

    fireEvent.doubleClick(screen.getByRole("button", { name: "新的研究对话" }));
    const input = screen.getByRole("textbox", { name: "重命名会话" });
    fireEvent.change(input, { target: { value: "更新后的标题" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRenameGeneralSession).toHaveBeenCalledWith("ses_new", "更新后的标题");

    const deleteButton = screen.getByRole("button", { name: "删除会话 旧的实验记录" });
    fireEvent.click(deleteButton);
    expect(onDeleteGeneralSession).not.toHaveBeenCalled();

    fireEvent.click(deleteButton);
    expect(onDeleteGeneralSession).toHaveBeenCalledWith("ses_old");
    expect(onDeleteGeneralSession).toHaveBeenCalledTimes(1);
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(confirm).toHaveBeenLastCalledWith("确定删除会话“旧的实验记录”吗？此操作不可恢复。");
  });

  it("普通项目模式只保留对话入口", () => {
    const onOpenGeneralWorkspace = vi.fn();
    renderShell("home", {
      activeDirectory: "/home/codexlab/DevTool/Alpha",
      isGeneralWorkspaceActive: false,
      onOpenGeneralWorkspace
    });

    expect(screen.queryByRole("button", { name: "新建对话" })).toBeNull();
    expect(screen.queryByRole("searchbox", { name: "搜索会话" })).toBeNull();
    expect(screen.queryByRole("button", { name: "新的研究对话" })).toBeNull();

    const conversationEntry = screen.getByRole("button", { name: "对话" });
    expect(conversationEntry.getAttribute("aria-current")).toBeNull();
    fireEvent.click(conversationEntry);
    expect(onOpenGeneralWorkspace).toHaveBeenCalledTimes(1);
  });

  it("移动抽屉可由菜单、关闭按钮和背景控制，并恢复菜单按钮焦点", () => {
    renderShell();

    const menuButton = screen.getByRole("button", { name: "打开导航" });
    expect(menuButton.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(menuButton);
    const dialog = screen.getByRole("dialog", { name: "平台导航" });
    const closeButton = within(dialog).getByRole("button", { name: "关闭导航" });
    expect(menuButton.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(closeButton);

    fireEvent.click(closeButton);
    expect(screen.queryByRole("dialog", { name: "平台导航" })).toBeNull();
    expect(document.activeElement).toBe(menuButton);

    fireEvent.click(menuButton);
    fireEvent.click(screen.getByRole("button", { name: "关闭导航背景" }));
    expect(screen.queryByRole("dialog", { name: "平台导航" })).toBeNull();
    expect(document.activeElement).toBe(menuButton);
  });

  it("移动抽屉支持 Escape 关闭并恢复菜单按钮焦点", () => {
    renderShell();

    const menuButton = screen.getByRole("button", { name: "打开导航" });
    fireEvent.click(menuButton);
    expect(screen.getByRole("dialog", { name: "平台导航" })).not.toBeNull();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "平台导航" })).toBeNull();
    expect(document.activeElement).toBe(menuButton);
  });

  it("移动抽屉中的项目菜单优先处理 Escape，不会连带关闭抽屉", () => {
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "打开导航" }));
    const dialog = screen.getByRole("dialog", { name: "平台导航" });
    fireEvent.click(within(dialog).getByRole("button", { name: "展开项目" }));
    const projectMenuButton = within(dialog).getByRole("button", {
      name: "打开 Alpha 项目菜单"
    });
    fireEvent.click(projectMenuButton);
    expect(screen.getByRole("menu", { name: "Alpha 项目操作" })).not.toBeNull();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Alpha 项目操作" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "平台导航" })).not.toBeNull();
    expect(document.activeElement).toBe(projectMenuButton);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "平台导航" })).toBeNull();
  });

  it("移动抽屉完成模块导航后关闭，且业务回调只执行一次", () => {
    const onOpenKnowledge = vi.fn();
    const groups = createModuleGroups();
    const knowledge = groups[0]?.items[0];
    if (!knowledge) {
      throw new Error("未找到知识检索测试模块");
    }
    knowledge.onClick = onOpenKnowledge;
    renderShell("home", { moduleGroups: groups });

    fireEvent.click(screen.getByRole("button", { name: "打开导航" }));
    const dialog = screen.getByRole("dialog", { name: "平台导航" });
    fireEvent.click(within(dialog).getByRole("button", { name: "材料发现 Discover" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "知识检索" }));

    expect(onOpenKnowledge).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog", { name: "平台导航" })).toBeNull();
  });

  it("桌面和移动侧边栏共享分组、项目展开与会话搜索状态", () => {
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索会话" }), {
      target: { value: "实验" }
    });

    fireEvent.click(screen.getByRole("button", { name: "打开导航" }));
    const dialog = screen.getByRole("dialog", { name: "平台导航" });
    expect(
      within(dialog)
        .getByRole("button", { name: "材料发现 Discover" })
        .getAttribute("aria-expanded")
    ).toBe("true");
    expect(within(dialog).getByRole("button", { name: "收起项目" })).not.toBeNull();
    expect(
      (within(dialog).getByRole("searchbox", { name: "搜索会话" }) as HTMLInputElement)
        .value
    ).toBe("实验");

    fireEvent.click(within(dialog).getByRole("button", { name: "实验优化 Optimize" }));
    expect(
      screen
        .getAllByRole("button", { name: "实验优化 Optimize" })
        .every((button) => button.getAttribute("aria-expanded") === "true")
    ).toBe(true);

    fireEvent.change(within(dialog).getByRole("searchbox", { name: "搜索会话" }), {
      target: { value: "新的" }
    });
    expect(
      screen
        .getAllByRole("searchbox", { name: "搜索会话" })
        .every((input) => (input as HTMLInputElement).value === "新的")
    ).toBe(true);

    fireEvent.click(within(dialog).getByRole("button", { name: "收起项目" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭导航" }));
    expect(screen.getByRole("button", { name: "展开项目" })).not.toBeNull();
    expect(
      (screen.getByRole("searchbox", { name: "搜索会话" }) as HTMLInputElement).value
    ).toBe("新的");
  });

  it("视口切换到桌面时自动关闭移动抽屉", () => {
    let viewportListener: ((event: MediaQueryListEvent) => void) | null = null;
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((media: string) => ({
        matches: false,
        media,
        onchange: null,
        addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
          viewportListener = listener;
        },
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn()
      }))
    );
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "打开导航" }));
    expect(screen.getByRole("dialog", { name: "平台导航" })).not.toBeNull();

    act(() => {
      const listener = viewportListener as ((event: MediaQueryListEvent) => void) | null;
      listener?.({ matches: true } as MediaQueryListEvent);
    });
    expect(screen.queryByRole("dialog", { name: "平台导航" })).toBeNull();
  });

  it("长项目名和长会话名保持完整数据并由条目负责截断", () => {
    const longProjectName = "面向极端环境耐高温聚酰亚胺材料的超长项目名称";
    const longSessionTitle = "关于多尺度模拟、实验验证和配方迭代的超长研究会话名称";
    const longProjects: OpenScienceProjectSummary[] = [
      {
        directory: "/home/codexlab/DevTool/LongProjectDirectory",
        name: longProjectName,
        displayPath: "~/DevTool/LongProjectDirectory/with/a/very/long/path",
        updatedAt: 300,
        favorite: false
      }
    ];
    const longSessions: OpenScienceGeneralSessionSummary[] = [
      { id: "ses_long", title: longSessionTitle, updatedAt: 300 }
    ];
    renderShell("home", {
      projects: longProjects,
      generalSessions: longSessions,
      activeGeneralSessionID: "ses_long"
    });
    fireEvent.click(screen.getByRole("button", { name: "展开项目" }));

    const projectButton = getProjectButton(longProjects[0]!.directory);
    const projectTitle = projectButton.querySelector(".np-sidebar-list-item__title");
    const sessionButton = screen.getByRole("button", { name: longSessionTitle });
    const sessionTitle = sessionButton.querySelector(".np-sidebar-list-item__title");

    expect(projectTitle?.textContent).toBe(longProjectName);
    expect(projectTitle?.classList.contains("np-sidebar-list-item__title")).toBe(true);
    expect(sessionTitle?.textContent).toBe(longSessionTitle);
    expect(sessionTitle?.getAttribute("title")).toBe(longSessionTitle);
  });

  it("导航守卫完成后只执行一次目标动作，并忽略等待中的重复激活", async () => {
    let resolveGuard: (() => void) | null = null;
    const beforeNavigate = vi.fn(
      () => new Promise<void>((resolve) => { resolveGuard = resolve; })
    );
    const moduleGroups = createModuleGroups();
    const targetAction = vi.fn();
    moduleGroups[0]!.items[1]!.onClick = targetAction;
    renderShell("structureWorkbench", { moduleGroups, beforeNavigate });

    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
    const target = screen.getByRole("button", { name: "数据库查询" });
    fireEvent.click(target);
    fireEvent.click(target);

    expect(beforeNavigate).toHaveBeenCalledTimes(1);
    expect(targetAction).not.toHaveBeenCalled();

    await act(async () => {
      resolveGuard?.();
      await Promise.resolve();
    });
    expect(targetAction).toHaveBeenCalledTimes(1);
  });

  it("导航守卫失败时仍执行原动作", async () => {
    const moduleGroups = createModuleGroups();
    const targetAction = vi.fn();
    moduleGroups[0]!.items[1]!.onClick = targetAction;
    renderShell("structureWorkbench", {
      moduleGroups,
      beforeNavigate: vi.fn().mockRejectedValue(new Error("sync failed"))
    });

    fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
    fireEvent.click(screen.getByRole("button", { name: "数据库查询" }));
    await act(async () => { await Promise.resolve(); });
    expect(targetAction).toHaveBeenCalledTimes(1);
  });

  it("导航守卫超过 1.5 秒后使用最后状态继续执行原动作", async () => {
    vi.useFakeTimers();
    try {
      const moduleGroups = createModuleGroups();
      const targetAction = vi.fn();
      moduleGroups[0]!.items[1]!.onClick = targetAction;
      renderShell("structureWorkbench", {
        moduleGroups,
        beforeNavigate: vi.fn(() => new Promise<void>(() => undefined))
      });

      fireEvent.click(screen.getByRole("button", { name: "材料发现 Discover" }));
      fireEvent.click(screen.getByRole("button", { name: "数据库查询" }));
      expect(targetAction).not.toHaveBeenCalled();
      await act(async () => {
        vi.advanceTimersByTime(1500);
        await Promise.resolve();
      });
      expect(targetAction).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
