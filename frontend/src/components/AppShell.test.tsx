/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell, type AppShellModuleGroup } from "./AppShell";
import type { OpenScienceProjectSummary } from "../lib/openScienceProjectBridge";

afterEach(() => {
  cleanup();
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

function renderShell(
  activeModule = "home",
  options?: {
    projects?: OpenScienceProjectSummary[];
    activeDirectory?: string | null;
    isProjectBridgeReady?: boolean;
    onOpenProject?: (directory: string) => void;
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
    >
      <div>页面内容</div>
    </AppShell>
  );
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
      >
        <div>页面内容</div>
      </AppShell>
    );

    expect(screen.getByRole("button", { name: "数据与知识" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "知识检索" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "逆向设计" })).toBeNull();
  });

  it("项目区域始终展开并按快照顺序显示项目", () => {
    renderShell();

    expect(screen.getByRole("heading", { name: "项目" })).not.toBeNull();
    expect(screen.getAllByRole("button", { name: /Alpha/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /Beta/ })).toHaveLength(1);

    const projectButtons = screen
      .getAllByRole("button")
      .filter((button) => button.getAttribute("data-project-directory"));
    expect(projectButtons.map((button) => button.getAttribute("data-project-directory"))).toEqual([
      "/home/codexlab/DevTool/Alpha",
      "/home/codexlab/DevTool/Beta"
    ]);
  });

  it("高亮当前项目并把点击目录交给上层", () => {
    const onOpenProject = vi.fn();
    renderShell("home", {
      activeDirectory: "/home/codexlab/DevTool/Beta",
      onOpenProject
    });

    const activeProject = screen.getByRole("button", { name: /Beta/ });
    expect(activeProject.getAttribute("aria-current")).toBe("page");

    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    expect(onOpenProject).toHaveBeenCalledWith("/home/codexlab/DevTool/Alpha");
  });

  it("桥接尚未就绪时显示低干扰加载状态", () => {
    renderShell("home", { projects: [], isProjectBridgeReady: false });

    expect(screen.getByText("正在同步项目…")).not.toBeNull();
  });
});
