/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell, type AppShellModuleGroup } from "./AppShell";
import type { OpenScienceGeneralSessionSummary } from "../lib/openScienceGeneralSessionBridge";
import type { OpenScienceProjectSummary } from "../lib/openScienceProjectBridge";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function createModuleGroups(activeId?: string): AppShellModuleGroup[] {
  return [
    {
      title: "数据与知识",
      items: [
        {
          id: "databaseQuery",
          label: "数据库查询",
          description: "查询数据库",
          route: "/database-query",
          icon: <span aria-hidden="true">Q</span>,
          isActive: activeId === "databaseQuery",
          onClick: vi.fn()
        },
        {
          id: "knowledge",
          label: "知识检索",
          description: "检索知识",
          route: "/knowledge",
          icon: <span aria-hidden="true">K</span>,
          isActive: activeId === "knowledge",
          onClick: vi.fn()
        }
      ]
    },
    {
      title: "设计与生成",
      items: [
        {
          id: "reverseDesign",
          label: "逆向设计",
          description: "逆向设计",
          route: "/reverse-design",
          icon: <span aria-hidden="true">D</span>,
          isActive: activeId === "reverseDesign",
          onClick: vi.fn()
        }
      ]
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
  }
) {
  return render(
    <AppShell
      activeModule={activeModule}
      moduleGroups={createModuleGroups(activeModule)}
      onOpenHome={vi.fn()}
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
  it("只保留顶部品牌入口，不再渲染重复的智聚万物按钮", () => {
    renderShell();

    expect(screen.getAllByRole("button", { name: "智聚万物" })).toHaveLength(1);
  });

  it("业务模块组初始收起，并且一次最多展开一个", () => {
    renderShell();

    const dataGroup = screen.getByRole("button", { name: "数据与知识" });
    const designGroup = screen.getByRole("button", { name: "设计与生成" });

    expect(dataGroup.getAttribute("aria-expanded")).toBe("false");
    expect(designGroup.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "数据库查询" })).toBeNull();

    fireEvent.click(dataGroup);
    expect(dataGroup.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "数据库查询" })).not.toBeNull();

    fireEvent.click(designGroup);
    expect(dataGroup.getAttribute("aria-expanded")).toBe("false");
    expect(designGroup.getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByRole("button", { name: "数据库查询" })).toBeNull();
    expect(screen.getByRole("button", { name: "逆向设计" })).not.toBeNull();

    fireEvent.click(designGroup);
    expect(designGroup.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "逆向设计" })).toBeNull();
  });

  it("进入业务模块时自动展开它所属的模块组", () => {
    const view = renderShell();

    view.rerender(
      <AppShell
        activeModule="knowledge"
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

    expect(screen.getByRole("button", { name: "数据与知识" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "知识检索" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "逆向设计" })).toBeNull();
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
});
