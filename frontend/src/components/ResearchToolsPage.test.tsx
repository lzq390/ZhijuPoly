/* @vitest-environment jsdom */

import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppShellModuleGroup } from "./sidebar/PlatformSidebar";
import { ResearchToolsPage } from "./ResearchToolsPage";

class TestResizeObserver {
  static instances: TestResizeObserver[] = [];
  target: Element | null = null;
  disconnect = vi.fn();
  constructor(private callback: ResizeObserverCallback) {
    TestResizeObserver.instances.push(this);
  }
  observe(target: Element) { this.target = target; }
  resize(width: number) {
    this.callback([{ target: this.target, contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
}

class TestPointerEvent extends MouseEvent {
  readonly pointerType: string;
  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerType = init.pointerType ?? "mouse";
  }
}

function createGroups(): AppShellModuleGroup[] {
  return [
    { id: "discover", label: "材料发现", secondaryLabel: "Discover", items: [
      ["knowledge", "知识检索"], ["polytaoGeneration", "聚合物生成"], ["monomerRetrosynthesis", "单体逆合成反推"],
      ["explorer", "聚合物相似性探索"], ["databaseQuery", "数据库查询"], ["databaseFilter", "数据库筛选"], ["database", "数据库分析"]
    ] },
    { id: "build", label: "材料设计", secondaryLabel: "Build", items: [
      ["homopolymerPrediction", "均聚物性质预测"], ["monomerPolymerization", "单体正向聚合"], ["mdSimulationDemo", "MD 模拟"],
      ["monomerMdSimulation", "单体 MD 模拟"], ["monomerDft", "单体 DFT"], ["conditionalGeneration", "条件聚合物生成"]
    ] },
    { id: "optimize", label: "实验优化", secondaryLabel: "Optimize", items: [
      ["reverseDesign", "Tg 逆向设计"], ["highThroughputWorkflowDemo", "高通量优化演示"]
    ] }
  ].map((group) => ({
    ...group,
    id: group.id as AppShellModuleGroup["id"],
    items: group.items.map(([id, label]) => ({ id, label, description: `${label}原描述`, route: `/${id}`, icon: <span aria-hidden="true">图标</span>, isActive: false, onClick: vi.fn() }))
  }));
}

function Harness({ groups }: { groups: AppShellModuleGroup[] }) {
  const [expanded, setExpanded] = useState<ReadonlySet<AppShellModuleGroup["id"]>>(new Set());
  const [collapsed, setCollapsed] = useState<ReadonlySet<AppShellModuleGroup["id"]>>(new Set());
  return <ResearchToolsPage groups={groups} expandedGroupIds={expanded} collapsedGroupIds={collapsed} onToggleGroup={(id) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  })} onToggleCollapsedGroup={(id) => setCollapsed((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  })} />;
}

function section(container: HTMLElement, id: string) {
  return container.querySelector<HTMLElement>(`[data-research-group="${id}"]`)!;
}

function visibleModuleCount(container: HTMLElement, id: string) {
  return section(container, id).querySelectorAll("[data-module-id]").length;
}

beforeEach(() => {
  TestResizeObserver.instances = [];
  vi.stubGlobal("ResizeObserver", TestResizeObserver);
  vi.stubGlobal("PointerEvent", TestPointerEvent);
  vi.stubGlobal("innerWidth", 1024);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ResearchToolsPage", () => {
  it("shows a single row per category and expands each category independently", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    expect(screen.getByRole("heading", { level: 1, name: "实验工具" })).toBeTruthy();
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([3, 3, 2]);
    expect(within(section(container, "optimize")).queryByRole("button", { expanded: false })).toBeNull();
    expect(screen.getByRole("button", { name: "材料发现：展开更多（4）" }).getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开更多（4）" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([7, 3, 2]);
    fireEvent.click(screen.getByRole("button", { name: "材料设计：展开更多（3）" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([7, 6, 2]);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：仅显示一行" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([3, 6, 2]);
  });

  it("fully collapses each category independently while retaining its heading and reopen control", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：收起" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([0, 3, 2]);
    expect(within(section(container, "discover")).getByRole("heading", { name: /^材料发现/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "材料发现：展开" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: "材料发现：展开更多（4）" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "实验优化：收起" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([0, 3, 0]);
    expect(within(section(container, "optimize")).getByRole("heading", { name: /^实验优化/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([3, 3, 0]);
    fireEvent.click(screen.getByRole("button", { name: "材料设计：收起" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([3, 0, 0]);
    expect(within(section(container, "build")).getByRole("heading", { name: /^材料设计/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "实验优化：展开" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "材料设计：展开" })).toBeTruthy();
  });

  it("removes fully collapsed cards from keyboard focus and stops their active preview", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    act(() => knowledge.focus());
    expect(container.querySelector("img")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "材料发现：收起" }));
    expect(visibleModuleCount(container, "discover")).toBe(0);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByRole("button", { name: "知识检索", hidden: true })).toBeNull();
    expect(knowledge.isConnected).toBe(false);
    const reopen = screen.getByRole("button", { name: "材料发现：展开" });
    act(() => reopen.focus());
    act(() => knowledge.focus());
    expect(document.activeElement).toBe(reopen);
    fireEvent.click(reopen);
    expect(visibleModuleCount(container, "discover")).toBe(3);
    expect(container.querySelector("img")).toBeNull();
  });

  it("restores each category's remembered row count after fully collapsing and reopening", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开更多（4）" }));
    fireEvent.click(screen.getByRole("button", { name: "材料设计：展开更多（3）" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([7, 6, 2]);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：收起" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([0, 6, 2]);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([7, 6, 2]);
    expect(screen.getByRole("button", { name: "材料发现：仅显示一行" }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "材料发现：仅显示一行" }));
    fireEvent.click(screen.getByRole("button", { name: "材料发现：收起" }));
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开" }));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([3, 6, 2]);
    expect(screen.getByRole("button", { name: "材料发现：展开更多（4）" })).toBeTruthy();
  });

  it("uses the observed content width at each breakpoint and updates the remaining count", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const observer = TestResizeObserver.instances[0];
    for (const [width, columns] of [[1280,4], [1279,3], [900,3], [899,2], [560,2], [559,1]]) {
      act(() => observer.resize(width));
      expect(container.querySelector("[data-research-columns]")?.getAttribute("data-research-columns")).toBe(String(columns));
      expect(visibleModuleCount(container, "discover")).toBe(columns);
      expect(screen.getByRole("button", { name: `材料发现：展开更多（${7-columns}）` })).toBeTruthy();
    }
    expect(visibleModuleCount(container, "optimize")).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "实验优化：展开更多（1）" }));
    expect(visibleModuleCount(container, "optimize")).toBe(2);
    act(() => observer.resize(560));
    expect(screen.queryByRole("button", { name: "实验优化：仅显示一行" })).toBeNull();
    act(() => observer.resize(559));
    expect(screen.getByRole("button", { name: "实验优化：仅显示一行" })).toBeTruthy();
  });

  it("removes collapsed cards from the DOM and keyboard focus order", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    expect(container.querySelector('[data-module-id="databaseQuery"]')).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开更多（4）" }));
    const queryCard = screen.getByRole("button", { name: "数据库查询" });
    act(() => queryCard.focus());
    expect(document.activeElement).toBe(queryCard);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：仅显示一行" }));
    expect(container.querySelector('[data-module-id="databaseQuery"]')).toBeNull();
    expect(document.activeElement).not.toBe(queryCard);
  });

  it("calls the existing action for each of the fifteen module buttons", () => {
    const groups = createGroups();
    render(<Harness groups={groups} />);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开更多（4）" }));
    fireEvent.click(screen.getByRole("button", { name: "材料设计：展开更多（3）" }));
    for (const item of groups.flatMap((group) => group.items)) {
      const button = screen.getByRole("button", { name: item.label });
      expect(button.getAttribute("data-module-id")).toBe(item.id);
      fireEvent.click(button);
      expect(item.onClick).toHaveBeenCalledTimes(1);
    }
  });

  it("automatically loads a hovered card's GIF while keeping the entire card as its module entry", () => {
    const groups = createGroups();
    const { container } = render(<Harness groups={groups} />);
    const card = screen.getByRole("button", { name: "知识检索" });
    expect(container.querySelector("img")).toBeNull();
    fireEvent.pointerEnter(card, { pointerType: "mouse" });
    const gif = container.querySelector("img")!;
    expect(gif.getAttribute("src")).toBe("/previews/research-tools/knowledge.gif");
    expect(gif.hidden).toBe(true);
    expect(groups[0].items[0].onClick).not.toHaveBeenCalled();
    const staticPreview = card.querySelector<HTMLElement>(".np-research-tool__static")!;
    expect(staticPreview.hidden).toBe(false);
    fireEvent.load(gif);
    expect(gif.hidden).toBe(false);
    expect(staticPreview.hidden).toBe(true);
    expect(container.querySelector("button button")).toBeNull();
    expect(card.closest("article")!.querySelectorAll("button")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /播放.*预览/ })).toBeNull();
    fireEvent.click(card);
    expect(groups[0].items[0].onClick).toHaveBeenCalledTimes(1);
  });

  it("plays only the hovered card and restores static previews when the pointer leaves", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    const generation = screen.getByRole("button", { name: "聚合物生成" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    fireEvent.pointerEnter(generation, { pointerType: "mouse" });
    expect(knowledge.querySelector("img")).toBeNull();
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/polytaoGeneration.gif");
    fireEvent.pointerLeave(generation, { pointerType: "mouse" });
    expect(container.querySelector("img")).toBeNull();
    expect(generation.querySelector<HTMLElement>(".np-research-tool__static")!.hidden).toBe(false);
  });

  it("previews keyboard focus and preserves whichever card remains hovered or focused", () => {
    const groups = createGroups();
    const { container } = render(<Harness groups={groups} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    const generation = screen.getByRole("button", { name: "聚合物生成" });
    act(() => knowledge.focus());
    const focusedGif = container.querySelector("img");
    expect(focusedGif?.getAttribute("src")).toBe("/previews/research-tools/knowledge.gif");
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    fireEvent.pointerLeave(knowledge, { pointerType: "mouse" });
    expect(container.querySelector("img")).toBe(focusedGif);
    fireEvent.pointerEnter(generation, { pointerType: "mouse" });
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/polytaoGeneration.gif");
    fireEvent.pointerLeave(generation, { pointerType: "mouse" });
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/knowledge.gif");
    act(() => knowledge.blur());
    expect(container.querySelector("img")).toBeNull();
    fireEvent.pointerEnter(generation, { pointerType: "mouse" });
    act(() => generation.focus());
    const hoveredGif = container.querySelector("img");
    act(() => generation.blur());
    expect(container.querySelector("img")).toBe(hoveredGif);
    fireEvent.pointerLeave(generation, { pointerType: "mouse" });
    expect(container.querySelector("img")).toBeNull();
    expect(groups.flatMap((group) => group.items).every((item) => !vi.mocked(item.onClick).mock.calls.length)).toBe(true);
  });

  it("ignores touch hover and focus while keeping a single tap as the module action", () => {
    const groups = createGroups();
    const { container } = render(<Harness groups={groups} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    fireEvent.pointerEnter(knowledge, { pointerType: "touch" });
    fireEvent.pointerDown(knowledge, { pointerType: "touch" });
    act(() => knowledge.focus());
    expect(container.querySelector("img")).toBeNull();
    fireEvent.click(knowledge);
    expect(groups[0].items[0].onClick).toHaveBeenCalledTimes(1);
    fireEvent.pointerLeave(knowledge, { pointerType: "touch" });
    expect(container.querySelector("img")).toBeNull();
  });

  it("restores keyboard previews after switching from touch to Tab navigation", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    const generation = screen.getByRole("button", { name: "聚合物生成" });
    fireEvent.pointerDown(knowledge, { pointerType: "touch" });
    act(() => knowledge.focus());
    expect(container.querySelector("img")).toBeNull();
    fireEvent.keyDown(knowledge, { key: "Tab" });
    act(() => generation.focus());
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/polytaoGeneration.gif");
  });

  it("ignores late load and error events from a preview after selecting another card", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    fireEvent.pointerEnter(screen.getByRole("button", { name: "知识检索" }), { pointerType: "mouse" });
    const previousGif = container.querySelector("img")!;
    fireEvent.pointerEnter(screen.getByRole("button", { name: "聚合物生成" }), { pointerType: "mouse" });
    const currentGif = container.querySelector("img")!;
    fireEvent.load(previousGif);
    expect(currentGif.hidden).toBe(true);
    fireEvent.load(currentGif);
    fireEvent.error(previousGif);
    expect(currentGif.hidden).toBe(false);
    expect(container.querySelector("img")).toBe(currentGif);
  });

  it("ignores old image events after leaving and returning to the same card", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    const previousGif = container.querySelector("img")!;
    fireEvent.pointerLeave(knowledge, { pointerType: "mouse" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    const currentGif = container.querySelector("img")!;
    expect(currentGif).not.toBe(previousGif);
    fireEvent.load(previousGif);
    fireEvent.error(previousGif);
    expect(container.querySelector("img")).toBe(currentGif);
    expect(currentGif.hidden).toBe(true);
    fireEvent.load(currentGif);
    expect(currentGif.hidden).toBe(false);
  });

  it("stops a preview hidden by collapsing a category without resuming it on expansion", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开更多（4）" }));
    fireEvent.pointerEnter(screen.getByRole("button", { name: "数据库查询" }), { pointerType: "mouse" });
    expect(container.querySelector("img")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "材料发现：仅显示一行" }));
    expect(container.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "材料发现：展开更多（4）" }));
    expect(container.querySelector("img")).toBeNull();
  });

  it("stops a preview when fewer columns hide the card", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    fireEvent.pointerEnter(screen.getByRole("button", { name: "单体逆合成反推" }), { pointerType: "mouse" });
    act(() => TestResizeObserver.instances[0].resize(899));
    expect(container.querySelector("img")).toBeNull();
    act(() => TestResizeObserver.instances[0].resize(900));
    expect(container.querySelector("img")).toBeNull();
  });

  it("stops when the document is hidden and keeps previews stopped when returning", () => {
    const { container, unmount } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    fireEvent(document, new Event("visibilitychange"));
    expect(container.querySelector("img")).toBeNull();
    hidden.mockReturnValue(false);
    fireEvent(document, new Event("visibilitychange"));
    expect(container.querySelector("img")).toBeNull();
    fireEvent.pointerLeave(knowledge, { pointerType: "mouse" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    expect(container.querySelector("img")).toBeTruthy();
    unmount();
    const fresh = render(<Harness groups={createGroups()} />);
    expect(fresh.container.querySelector("img")).toBeNull();
  });

  it("restores the static preview after an error and retries after leaving and reentering", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    fireEvent.error(container.querySelector("img")!);
    expect(container.querySelector("img")).toBeNull();
    expect(knowledge.querySelector<HTMLElement>(".np-research-tool__static")!.hidden).toBe(false);
    expect(screen.queryByText("点击重试")).toBeNull();
    fireEvent.pointerMove(knowledge, { pointerType: "mouse" });
    expect(container.querySelector("img")).toBeNull();
    fireEvent.pointerLeave(knowledge, { pointerType: "mouse" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    expect(container.querySelectorAll("img")).toHaveLength(1);
    fireEvent.load(container.querySelector("img")!);
    expect(knowledge.querySelector<HTMLElement>(".np-research-tool__static")!.hidden).toBe(true);
  });

  it("resumes the focused card when leaving a different card whose preview failed", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    const generation = screen.getByRole("button", { name: "聚合物生成" });
    act(() => knowledge.focus());
    fireEvent.pointerEnter(generation, { pointerType: "mouse" });
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/polytaoGeneration.gif");
    fireEvent.error(container.querySelector("img")!);
    expect(container.querySelector("img")).toBeNull();
    fireEvent.pointerLeave(generation, { pointerType: "mouse" });
    expect(document.activeElement).toBe(knowledge);
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/knowledge.gif");
    expect(generation.querySelector("img")).toBeNull();
  });

  it("resumes the hovered card when blurring a different card whose preview failed", () => {
    const { container } = render(<Harness groups={createGroups()} />);
    const knowledge = screen.getByRole("button", { name: "知识检索" });
    const generation = screen.getByRole("button", { name: "聚合物生成" });
    fireEvent.pointerEnter(knowledge, { pointerType: "mouse" });
    act(() => generation.focus());
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/polytaoGeneration.gif");
    fireEvent.error(container.querySelector("img")!);
    expect(container.querySelector("img")).toBeNull();
    act(() => generation.blur());
    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/previews/research-tools/knowledge.gif");
    expect(generation.querySelector("img")).toBeNull();
  });

  it("honors externally retained expansion state and releases the observer on unmount", () => {
    const { container, unmount } = render(<ResearchToolsPage groups={createGroups()} expandedGroupIds={new Set(["discover"])} collapsedGroupIds={new Set()} onToggleGroup={vi.fn()} onToggleCollapsedGroup={vi.fn()} />);
    expect(visibleModuleCount(container, "discover")).toBe(7);
    expect(visibleModuleCount(container, "build")).toBe(3);
    const observer = TestResizeObserver.instances[0];
    unmount();
    expect(observer.disconnect).toHaveBeenCalledTimes(1);
  });

  it("falls back to resize measurements when ResizeObserver is unavailable", () => {
    vi.stubGlobal("ResizeObserver", undefined);
    const { container } = render(<Harness groups={createGroups()} />);
    vi.stubGlobal("innerWidth", 390);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(["discover", "build", "optimize"].map((id) => visibleModuleCount(container, id))).toEqual([1,1,1]);
  });
});
