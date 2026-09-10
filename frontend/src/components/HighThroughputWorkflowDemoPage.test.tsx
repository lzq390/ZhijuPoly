/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { highThroughputDemoScenario, type HighThroughputTargetKey } from "../constants/highThroughputDemoScenario";
import { HighThroughputWorkflowDemoPage } from "./HighThroughputWorkflowDemoPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("HighThroughputWorkflowDemoPage S0", () => {
  it.each([
    ["小数", ["250.25", "27.75", "26.75", "2.91"]],
    ["千位数", ["1250", "1000", "1000", "1000.0"]],
    ["微小值", ["1e-25", "1e-25", "1e-25", "1e-25"]],
  ])("返回 S0 保留%s阈值，不舍入或将千位分隔符写入数值输入", (_label, values) => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    highThroughputDemoScenario.targets.forEach((target, index) => {
      fireEvent.change(screen.getByRole("spinbutton", { name: `${target.shortLabel} 目标值` }), { target: { value: values[index] } });
    });
    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
    expect(view.container.querySelector('[data-agent-theme="modulus"] .ht-s1-agent-target')?.textContent)
      .toContain(values[3] === "1000.0" ? "1,000.0" : values[3]);
    fireEvent.click(screen.getByRole("button", { name: "返回 S0 场景设置" }));
    highThroughputDemoScenario.targets.forEach((target, index) => {
      const input = screen.getByRole<HTMLInputElement>("spinbutton", { name: `${target.shortLabel} 目标值` });
      expect(input.value).toBe(values[index]);
      expect(input.valueAsNumber).toBe(Number(values[index]));
    });
  });

  it("将场景设置作为独立首阶段，并保留可交互的演示输入", () => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);

    expect(screen.getByRole("heading", { level: 1, name: "高通量优化演示" })).not.toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "材料体系与目标设置" })).not.toBeNull();
    expect(view.container.querySelector(".ht-s0-page")).not.toBeNull();
    const toolbar = view.container.querySelector(".ht-workbench-toolbar");
    const board = view.container.querySelector(".ht-s0-board");
    expect(toolbar).not.toBeNull();
    expect(board).not.toBeNull();
    const scrollRegion = view.container.querySelector(".ht-scroll-region");
    expect(toolbar?.nextElementSibling).toBe(scrollRegion);
    expect(scrollRegion?.firstElementChild).toBe(board);
    expect(scrollRegion?.contains(toolbar)).toBe(false);
    expect(scrollRegion?.contains(screen.getByRole("heading", { level: 1 }))).toBe(false);
    expect(Array.from(board?.children ?? []).map((element) => element.className)).toEqual([
      "ht-workbench-header",
      "ht-flow-control-bar",
      "ht-scenario-panel",
    ]);
    expect(screen.getByRole("status").textContent).toContain("固定演示");
    expect(screen.getAllByRole("combobox")).toHaveLength(3);

    const monomerACount = screen.getByRole("spinbutton", { name: "单体 A 候选数量" });
    const monomerBCount = screen.getByRole("spinbutton", { name: "单体 B 候选数量" });
    const cteToggle = screen.getByRole("button", { name: "CTE" });

    expect((monomerACount as HTMLInputElement).value).toBe("120");
    expect((monomerBCount as HTMLInputElement).value).toBe("80");
    expect(cteToggle.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("spinbutton", { name: "CTE 目标值" })).not.toBeNull();
    expect(screen.queryByText("Tg Agent")).toBeNull();

    fireEvent.change(monomerACount, { target: { value: "96" } });
    fireEvent.change(monomerBCount, { target: { value: "64" } });
    expect(screen.getByText("96 × 64 = 6,144")).not.toBeNull();

    fireEvent.click(cteToggle);
    expect(cteToggle.getAttribute("aria-pressed")).toBe("false");
    expect(screen.queryByRole("spinbutton", { name: "CTE 目标值" })).toBeNull();
    expect(screen.getByText("已选择 3 个目标性质")).not.toBeNull();
  });

  it("可恢复默认参数，并通过场景确认进入同款工作台 S1", async () => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    const monomerACount = screen.getByRole("spinbutton", { name: "单体 A 候选数量" });

    fireEvent.change(monomerACount, { target: { value: "42" } });
    fireEvent.click(screen.getByRole("button", { name: "恢复默认场景参数" }));
    await waitFor(() => expect((monomerACount as HTMLInputElement).value).toBe("120"));

    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));

    expect(screen.queryByRole("heading", { level: 2, name: "材料体系与目标设置" })).toBeNull();
    expect(view.container.querySelector(".ht-s0-page")).toBeNull();
    expect(view.container.querySelector(".ht-s0-board")).toBeNull();
    expect(view.container.querySelector(".ht-workbench-toolbar")).not.toBeNull();
    expect(view.container.querySelector(".ht-s1-board.np-sw-accented-surface")).not.toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "正交实验与先验导入" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "展开 Tg Agent" })).not.toBeNull();
    expect(screen.getByText("S1", { selector: "[aria-current='step'] span" })).not.toBeNull();
  });

  it("支持键盘展开并选择同款工作台下拉选项，关闭后保留焦点", async () => {
    render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    const materialSelect = screen.getByRole("combobox", { name: "材料类型" });

    materialSelect.focus();
    fireEvent.keyDown(materialSelect, { key: "ArrowDown" });
    expect(materialSelect.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("listbox", { name: "材料类型" })).not.toBeNull();
    const option = screen.getByRole("option", { name: /Polyimide.*聚酰亚胺/ });
    expect(option.getAttribute("aria-selected")).toBe("true");
    expect(materialSelect.getAttribute("aria-activedescendant")).toBe(option.id);

    fireEvent.keyDown(materialSelect, { key: "Enter" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(materialSelect.textContent).toContain("Polyimide");
    await waitFor(() => expect(document.activeElement).toBe(materialSelect));

    fireEvent.click(materialSelect);
    fireEvent.keyDown(materialSelect, { key: "Escape" });
    expect(materialSelect.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(materialSelect);

    fireEvent.click(materialSelect);
    fireEvent.keyDown(materialSelect, { key: "Tab" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("S0/S1 共用一个内部滚动区，切换阶段回到顶部并聚焦工作面标题", () => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    const scrollRegion = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    scrollRegion.scrollTop = 160;
    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(view.container.querySelector(".ht-scroll-region")).toBe(scrollRegion);
    expect(scrollRegion.scrollTop).toBe(0);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "正交实验与先验导入" }));

    scrollRegion.scrollTop = 600;
    fireEvent.click(screen.getByRole("button", { name: "返回 S0 场景设置" }));
    expect(scrollRegion.scrollTop).toBe(0);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "材料体系与目标设置" }));
  });

  it("点击页面其它位置关闭下拉，目标阈值提供方向和单位并保留编辑", () => {
    render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    const representationSelect = screen.getByRole("combobox", { name: "空间表征" });
    fireEvent.click(representationSelect);
    expect(screen.getByRole("option", { name: /PolyBERT/ })).not.toBeNull();

    const cteInput = screen.getByRole("spinbutton", { name: "CTE 目标值", description: "不超过 ppm/K" });
    fireEvent.pointerDown(cteInput);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(cteInput.getAttribute("aria-describedby")).toBe("ht-target-direction-cte ht-target-unit-cte");
    expect(document.getElementById("ht-target-direction-cte")?.textContent).toContain("≤");
    expect(document.getElementById("ht-target-unit-cte")?.textContent).toContain("ppm/K");
    fireEvent.change(cteInput, { target: { value: "30" } });
    expect((cteInput as HTMLInputElement).value).toBe("30");
  });
});

function enterPriorStage() {
  const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
  fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
  return view;
}

function uploadSample(key: HighThroughputTargetKey, fileName?: string) {
  const target = highThroughputDemoScenario.targets.find((item) => item.key === key)!;
  const expand = screen.queryByRole("button", { name: `展开 ${target.shortLabel} Agent` });
  if (expand) fireEvent.click(expand);
  const csv = highThroughputDemoScenario.doeCsvFiles[key];
  fireEvent.change(screen.getByLabelText(`上传 ${target.shortLabel} CSV 文件`), {
    target: { files: [new File(["fixed-demo"], fileName ?? csv.fileName, { type: "text/csv" })] },
  });
}

describe("HighThroughputWorkflowDemoPage S1", () => {
  it("沿用 S0 外框和阶段导航，默认只显示 Agent 紧凑入口", () => {
    const view = enterPriorStage();
    const board = view.container.querySelector(".ht-workbench-board")!;
    expect(screen.getByRole("heading", { level: 1, name: "高通量优化演示" })).not.toBeNull();
    expect(board.querySelectorAll(".ht-s1-agent-card")).toHaveLength(4);
    expect(board.querySelectorAll(".ht-flow-step")).toHaveLength(7);
    expect(view.container.querySelector(".ht-agent-orbit")).toBeNull();
    expect(view.container.querySelector(".ht-agent-attention-overlay")).toBeNull();
    expect(screen.getByText(/当前按文件名匹配预设数据，不解析上传内容/)).not.toBeNull();
    expect(board.querySelectorAll(".ht-s1-agent-toggle")).toHaveLength(4);
    expect(board.querySelector(".ht-s1-agent-rail")).toBeNull();
    for (const target of highThroughputDemoScenario.targets) {
      expect(screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` }).getAttribute("aria-expanded")).toBe("false");
      const toggle = screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` });
      expect(toggle.textContent).toBe("");
      expect(toggle.getAttribute("title")).toBe(`展开 ${target.shortLabel} Agent`);
      expect(toggle.querySelectorAll("svg")).toHaveLength(1);
      const panel = screen.getByRole("complementary", { name: `${target.shortLabel} Agent 面板` });
      expect(panel.querySelectorAll(".ht-s1-agent-card")).toHaveLength(1);
    }
    expect(screen.queryByRole("button", { name: "查看 Tg Agent" })).toBeNull();
    expect(screen.queryAllByRole("link", { name: /下载 .* 样例 CSV/ })).toHaveLength(0);
    expect(board.querySelectorAll(".ht-s1-agent-flyout[hidden]")).toHaveLength(4);
    for (const target of highThroughputDemoScenario.targets) {
      fireEvent.click(screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` }));
    }
    expect(screen.queryAllByRole("link", { name: /下载 .* 样例 CSV/ })).toHaveLength(0);
    expect(screen.getAllByRole("button", { name: /上传样例（/ })).toHaveLength(4);
    for (const target of highThroughputDemoScenario.targets) {
      const panel = screen.getByRole("complementary", { name: `${target.shortLabel} Agent 面板` });
      const actions = panel.querySelector(".ht-s1-upload-actions") as HTMLElement;
      expect(within(panel).queryByRole("link")).toBeNull();
      expect(within(actions).getByRole("button", { name: `上传 CSV（${target.shortLabel}）` })).not.toBeNull();
      expect(within(actions).getByRole("button", { name: `上传样例（${target.shortLabel}）` })).not.toBeNull();
    }
    expect(board.classList.contains("np-sw-accented-surface")).toBe(true);
    expect(board.querySelectorAll(".ht-s1-agent-flyout.np-sw-accented-surface")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /关闭 .* Agent/ })).toBeNull();
    expect(screen.getByRole("button", { name: "确认先验，进入 S2" }).hasAttribute("disabled")).toBe(true);
  });

  it("返回 S0 保留场景参数与上传状态，重新确认后仍回到 S1", () => {
    vi.useFakeTimers();
    render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    fireEvent.change(screen.getByRole("spinbutton", { name: "单体 A 候选数量" }), { target: { value: "96" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "CTE 目标值" }), { target: { value: "32" } });
    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
    uploadSample("tg");
    fireEvent.click(screen.getByRole("button", { name: "返回 S0 场景设置" }));

    expect((screen.getByRole("spinbutton", { name: "单体 A 候选数量" }) as HTMLInputElement).value).toBe("96");
    expect((screen.getByRole("spinbutton", { name: "CTE 目标值" }) as HTMLInputElement).value).toBe("32");
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText("S0", { selector: "[aria-current='step'] span" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
    expect(screen.queryByRole("button", { name: "查看 Tg Agent" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    expect(screen.getByText("先验已就绪")).not.toBeNull();
    expect(screen.getByText("7,680")).not.toBeNull();
    expect(screen.getByRole("table")).not.toBeNull();
  });

  it("主图使用分层卡头与内容区，目标标识和预览跟随 Agent 同步", () => {
    const view = enterPriorStage();
    const panel = screen.getByRole("region", { name: "候选空间" });
    expect(Array.from(panel.children).map((element) => element.className)).toEqual([
      "ht-s1-space-header", "ht-s1-space-body",
    ]);
    expect(panel.querySelector(".ht-s1-space-watermark")?.getAttribute("aria-hidden")).toBe("true");
    expect(panel.querySelectorAll(".ht-property-candidate-point").length).toBeGreaterThan(0);
    expect(panel.querySelector(".ht-property-space-backdrop")).not.toBeNull();
    const detailGrid = panel.querySelector(".ht-property-grid-detail")!;
    expect(detailGrid.getAttribute("aria-hidden")).toBe("true");
    expect(detailGrid.getAttribute("pointer-events")).toBe("none");
    const pattern = panel.querySelector("pattern")!;
    expect(pattern.getAttribute("patternUnits")).toBe("userSpaceOnUse");
    expect(detailGrid.getAttribute("fill")).toBe(`url(#${pattern.id})`);
    for (const target of highThroughputDemoScenario.targets) {
      fireEvent.click(screen.getByRole("tab", { name: target.shortLabel }));
      const focus = panel.querySelector<HTMLElement>(".ht-s1-space-focus")!;
      expect(focus.textContent).toBe(`当前目标 ${target.shortLabel}`);
      expect(focus.style.getPropertyValue("--target-color")).toBe(target.color);
      const preview = screen.getByRole("region", { name: `${target.shortLabel} 先验数据预览` });
      expect((preview as HTMLElement).style.getPropertyValue("--target-color")).toBe(target.color);
    }
    const progressColors = Array.from(view.container.querySelectorAll<HTMLElement>(".ht-s1-progress-track i")).map((item) => item.style.getPropertyValue("--target-color"));
    expect(progressColors).toEqual(highThroughputDemoScenario.targets.map((target) => target.color));
  });

  it("四个 Agent 独立开关，同侧互不捆绑，选择同步图表与预览", () => {
    enterPriorStage();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    expect(screen.queryByRole("button", { name: "查看 CTE Agent" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开 CTE Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 Modulus Agent" }));
    expect(screen.getByRole("button", { name: "查看 Tg Agent" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看 CTE Agent" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看 Modulus Agent" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "查看 Elongation Agent" })).toBeNull();
    const tgPanel = screen.getByRole("complementary", { name: "Tg Agent 面板" });
    expect(within(tgPanel).queryByRole("button", { name: "查看 CTE Agent" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "收起 CTE Agent" }));
    expect(screen.queryByRole("button", { name: "查看 CTE Agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "收起 Tg Agent" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "收起 Modulus Agent" }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "展开 CTE Agent" }));
    expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "收起 CTE Agent" }));
    expect(screen.getByRole("heading", { name: "CTE 先验数据预览" })).not.toBeNull();
  });

  it("Agent 外层背景可选中，但阈值和上传框不切换目标", () => {
    const view = enterPriorStage();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 CTE Agent" }));
    const panel = screen.getByRole("complementary", { name: "Tg Agent 面板" });
    const select = within(panel).getByRole("button", { name: "查看 Tg Agent" });

    for (const selector of [".ht-s1-agent-card", ".ht-s1-agent-header", ".ht-s1-agent-body", ".ht-s1-agent-status"]) {
      fireEvent.click(screen.getByRole("tab", { name: "CTE" }));
      expect(select.getAttribute("aria-pressed")).toBe("false");
      fireEvent.click(panel.querySelector(selector)!);
      expect(select.getAttribute("aria-pressed")).toBe("true");
      expect(panel.querySelector(".ht-s1-agent-card.selected")).not.toBeNull();
      expect(screen.getByRole("tab", { name: "Tg" }).getAttribute("aria-selected")).toBe("true");
      expect(screen.getByRole("heading", { name: "Tg 先验数据预览" })).not.toBeNull();
      expect(view.container.querySelectorAll('.ht-s1-agent-toggle[aria-expanded="true"]')).toHaveLength(2);
      expect(document.activeElement).toBe(select);
    }

    const cteSelect = screen.getByRole("button", { name: "查看 CTE Agent" });
    fireEvent.click(cteSelect);
    cteSelect.focus();
    for (const selector of [".ht-s1-agent-target", ".ht-s1-agent-target strong", ".ht-s1-agent-target-label svg", ".ht-s1-upload-area", ".ht-s1-file-summary", ".ht-s1-upload-meta span"]) {
      fireEvent.click(panel.querySelector(selector)!);
      expect(select.getAttribute("aria-pressed")).toBe("false");
      expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
      expect(screen.getByRole("heading", { name: "CTE 先验数据预览" })).not.toBeNull();
      expect(document.activeElement).toBe(cteSelect);
    }
  });

  it("整卡点击不截获上传按钮和文件输入的事件，样例仍只导入对应 Agent", () => {
    vi.useFakeTimers();
    enterPriorStage();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 CTE Agent" }));
    const panel = screen.getByRole("complementary", { name: "Tg Agent 面板" });
    const input = within(panel).getByLabelText("上传 Tg CSV 文件");
    const chooseFile = vi.spyOn(input, "click");
    fireEvent.click(within(panel).getByRole("button", { name: "上传 CSV（Tg）" }).querySelector("svg")!);
    expect(chooseFile).toHaveBeenCalledOnce();
    expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getByRole("status").textContent).toBe("未上传");

    fireEvent.click(within(panel).getByRole("button", { name: "上传样例（Tg）" }));
    expect(screen.getByRole("tab", { name: "Tg" }).getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getByRole("status").textContent).toBe("载入中…");
    act(() => vi.advanceTimersByTime(1000));
    expect(within(panel).getByRole("status").textContent).toBe("先验已就绪");
    expect(within(screen.getByRole("complementary", { name: "CTE Agent 面板" })).getByRole("status").textContent).toBe("未上传");
    expect(chooseFile).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("tab", { name: "CTE" }));
    fireEvent.click(panel.querySelector(".ht-s1-file-name")!);
    expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
  });

  it("目标标签支持方向键、首尾键与 roving tabindex，Agent 选择同步预览", () => {
    enterPriorStage();
    const tg = screen.getByRole("tab", { name: "Tg" });
    tg.focus();
    fireEvent.keyDown(tg, { key: "ArrowLeft" });
    const modulus = screen.getByRole("tab", { name: "Modulus" });
    expect(document.activeElement).toBe(modulus);
    expect(modulus.getAttribute("aria-selected")).toBe("true");
    expect(tg.tabIndex).toBe(-1);
    fireEvent.keyDown(modulus, { key: "Home" });
    expect(document.activeElement).toBe(tg);
    fireEvent.keyDown(tg, { key: "End" });
    expect(document.activeElement).toBe(modulus);
    fireEvent.click(screen.getByRole("button", { name: "展开 Elongation Agent" }));
    expect(screen.getByRole("tab", { name: "Elongation" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel", { name: "Elongation" })).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Elongation 先验数据预览" })).not.toBeNull();
  });

  it("只通过贴边箭头收起展开，保留入口焦点且不再显示关闭按钮", () => {
    enterPriorStage();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    const toggle = screen.getByRole("button", { name: "收起 Tg Agent" });
    const agent = screen.getByRole("button", { name: "查看 Tg Agent" });
    expect(document.activeElement).toBe(toggle);
    expect(screen.queryByRole("button", { name: /关闭 .* Agent/ })).toBeNull();
    // The card is a persistent disclosure, not a dismissible dialog.
    fireEvent.keyDown(agent, { key: "Escape" });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(agent);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(screen.queryByRole("button", { name: "查看 Tg Agent" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "展开 Tg Agent" }));

    fireEvent.click(screen.getByRole("button", { name: "展开 Elongation Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 Modulus Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "收起 Modulus Agent" }));
    expect(screen.queryByRole("button", { name: "查看 Modulus Agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看 Elongation Agent" })).not.toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "展开 Modulus Agent" }));
  });

  it("Agent 使用统一标识、方向感知目标与 CSV 信息，不暗示在线运行", () => {
    enterPriorStage();
    for (const [index, target] of highThroughputDemoScenario.targets.entries()) {
      fireEvent.click(screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` }));
      const panel = screen.getByRole("complementary", { name: `${target.shortLabel} Agent 面板` });
      expect(panel.getAttribute("data-agent-theme")).toBe(target.key);
      expect((panel as HTMLElement).style.getPropertyValue("--target-color")).toBe(target.color);
      expect(panel.querySelector(".ht-s1-agent-watermark")?.getAttribute("aria-hidden")).toBe("true");
      expect(panel.querySelector(".ht-s1-agent-id")?.textContent).toBe(`AGENT ${String(index + 1).padStart(2, "0")}`);
      expect(within(panel).getByText("当前查看")).not.toBeNull();
      expect(within(panel).getByText(target.direction === "higher" ? "越高越好" : "越低越好")).not.toBeNull();
      expect(panel.querySelector(".ht-s1-agent-target")?.textContent).toContain(target.unit === "degC" ? "°C" : target.unit);
      expect(within(panel).getByText("尚未上传先验数据")).not.toBeNull();
      expect(panel.querySelector(".ht-s1-file-name")).toBeNull();
      expect(panel.textContent).not.toContain(highThroughputDemoScenario.doeCsvFiles[target.key].fileName);
      expect(panel.textContent).not.toContain("12 条 DOE 样本");
      expect(within(panel).getByRole("status").textContent).toBe("未上传");
    }
  });

  it.each([334, 676])("窄屏宽度 %i 一次只展开一个 Agent，避免浮层相互遮挡", (width) => {
    const view = enterPriorStage();
    Object.defineProperty(view.container.querySelector(".ht-s1-agent-layout"), "clientWidth", { value: width });
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    expect(screen.getByRole("button", { name: "查看 Tg Agent" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开 Modulus Agent" }));
    expect(screen.queryByRole("button", { name: "查看 Tg Agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看 Modulus Agent" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "展开 Tg Agent" }).getAttribute("aria-expanded")).toBe("false");
  });

  it("2K 内覆模式同侧互斥，左右独立展开且保留已载入的先验", () => {
    vi.useFakeTimers();
    const view = enterPriorStage();
    const layout = view.container.querySelector<HTMLElement>(".ht-s1-agent-layout")!;
    Object.defineProperty(layout, "clientWidth", { value: 1494 });
    layout.style.setProperty("--ht-s1-inset-agents", "1");

    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "上传样例（Tg）" }));
    act(() => vi.advanceTimersByTime(1000));
    fireEvent.click(screen.getByRole("button", { name: "展开 Elongation Agent" }));
    expect(screen.getByRole("button", { name: "查看 Tg Agent" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看 Elongation Agent" })).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "展开 CTE Agent" }));
    expect(screen.queryByRole("button", { name: "查看 Tg Agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看 Elongation Agent" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开 Modulus Agent" }));
    expect(screen.queryByRole("button", { name: "查看 Elongation Agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看 CTE Agent" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看 Modulus Agent" })).not.toBeNull();
    expect(view.container.querySelectorAll('.ht-s1-agent-toggle[aria-expanded="true"]')).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    expect(screen.queryByRole("button", { name: "查看 CTE Agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看 Modulus Agent" })).not.toBeNull();
    expect(screen.getByRole("table")).not.toBeNull();
    expect(within(screen.getByRole("complementary", { name: "Tg Agent 面板" })).getByRole("status").textContent).toBe("先验已就绪");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "收起 Tg Agent" }));
  });

  it("按真实绘图区定位内覆卡片，外部空间变化时保留焦点并重新约束展开状态", () => {
    let resize = () => {};
    const observe = vi.fn();
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) {
        resize = () => callback([], this as unknown as ResizeObserver);
      }
      observe = observe;
      disconnect = disconnect;
    });
    const view = enterPriorStage();
    const layout = view.container.querySelector<HTMLElement>(".ht-s1-agent-layout")!;
    const map = view.container.querySelector<HTMLElement>(".ht-s1-map")!;
    Object.defineProperty(layout, "clientWidth", { value: 1494 });
    const plotRect = vi.spyOn(map, "getBoundingClientRect").mockReturnValue(new DOMRect(500, 720, 1402, 674));
    const agents = Array.from(layout.querySelectorAll<HTMLElement>(".ht-s1-agent-disclosure"));
    for (const [index, agent] of agents.entries()) {
      vi.spyOn(agent, "getBoundingClientRect").mockReturnValue(new DOMRect(0, index % 2 === 0 ? 500 : 950, 22, 450));
    }
    expect(observe).toHaveBeenCalledWith(layout);
    expect(observe).toHaveBeenCalledWith(map);
    expect(observe).toHaveBeenCalledWith(view.container.querySelector(".ht-workbench-page"));

    for (const target of highThroughputDemoScenario.targets) {
      fireEvent.click(screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` }));
    }
    const focused = screen.getByRole("button", { name: "查看 Tg Agent" });
    focused.focus();
    layout.style.setProperty("--ht-s1-inset-agents", "1");
    act(resize);
    expect(document.activeElement).toBe(focused);
    expect(screen.getByRole("button", { name: "查看 Tg Agent" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看 Modulus Agent" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "查看 CTE Agent" })).toBeNull();
    expect(screen.queryByRole("button", { name: "查看 Elongation Agent" })).toBeNull();
    expect(layout.style.getPropertyValue("--ht-s1-inset-height")).toBe("650px");
    expect(agents.map((agent) => agent.style.getPropertyValue("--ht-s1-inset-anchor"))).toEqual(["232px", "432px", "232px", "432px"]);

    plotRect.mockReturnValue(new DOMRect(500, 760, 1402, 700));
    act(resize);
    expect(layout.style.getPropertyValue("--ht-s1-inset-height")).toBe("676px");
    expect(agents.map((agent) => agent.style.getPropertyValue("--ht-s1-inset-anchor"))).toEqual(["272px", "498px", "272px", "498px"]);

    layout.style.setProperty("--ht-s1-inset-agents", "0");
    act(resize);
    fireEvent.click(screen.getByRole("button", { name: "展开 CTE Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "展开 Elongation Agent" }));
    expect(view.container.querySelectorAll('.ht-s1-agent-toggle[aria-expanded="true"]')).toHaveLength(4);
    view.unmount();
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it("保留原有样例匹配与四份门禁，上传错误就地提示且可重试", () => {
    vi.useFakeTimers();
    const view = enterPriorStage();
    uploadSample("tg", "wrong-name.csv");
    expect(screen.getByRole("alert").textContent).toContain("文件不匹配");
    const agent = screen.getByRole("complementary", { name: "Tg Agent 面板" });
    expect(agent.querySelector(".ht-s1-file-name")?.textContent).toBe("wrong-name.csv");
    expect(within(agent).getByText("请重新上传或使用样例")).not.toBeNull();
    expect(agent.textContent).not.toContain("已上传");
    expect(agent.textContent).not.toContain("12 条 DOE 样本");
    expect(screen.queryByRole("table")).toBeNull();
    uploadSample("tg");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("正在载入 DOE 样本")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getAllByRole("row")).toHaveLength(13);
    expect(view.container.querySelectorAll(".ht-doe-sample")).toHaveLength(12);
    expect(screen.getByRole("button", { name: "确认先验，进入 S2" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "展开结构字段" }));
    expect(screen.getByRole("columnheader", { name: "Polymer SMILES" })).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "Cluster" })).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "收起结构字段" }));
    expect(screen.queryByRole("columnheader", { name: "Polymer SMILES" })).toBeNull();

    for (const key of ["cte", "elongation", "modulus"] as const) uploadSample(key);
    act(() => vi.advanceTimersByTime(1000));
    const next = screen.getByRole("button", { name: "确认先验，进入 S2" });
    expect(next.hasAttribute("disabled")).toBe(false);
    fireEvent.click(next);
    expect(screen.getByRole("button", { name: "返回 S0 场景设置" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "重新上传 CSV（Tg）" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getAllByRole("button", { name: /上传样例（/ }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    expect(screen.getByRole("button", { name: "正在生成先验热点" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("tab", { name: "Modulus" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(agent.querySelector(".ht-s1-agent-card")!);
    expect(screen.getByRole("tab", { name: "Modulus" }).getAttribute("aria-selected")).toBe("true");
    expect(agent.querySelector(".ht-s1-agent-card")?.getAttribute("data-disabled")).toBe("true");
    const scrollRegion = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    scrollRegion.scrollTop = 400;
    act(() => vi.advanceTimersByTime(900));
    expect(screen.getByText("S2", { selector: "[aria-current='step'] span" })).not.toBeNull();
    expect(view.container.querySelector(".ht-s1-page")).toBeNull();
    expect(view.container.querySelector(".ht-scroll-region")).toBe(scrollRegion);
    expect(scrollRegion.scrollTop).toBe(0);
    expect(view.container.querySelectorAll(".ht-agent-orbit")).toHaveLength(0);
    expect(view.container.querySelector(".ht-s2-board")).not.toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "先验热点与推荐验证" }));
    expect(view.container.querySelector(".ht-property-candidate-point")?.getAttribute("opacity")).toBe("0.34");
    expect(view.container.querySelector(".ht-property-space-backdrop")?.getAttribute("fill")).toBe("#fbfdff");
  });

  it.each(["CSV", "样例"])("%s 入口明确区分未上传、载入中与已上传，重置后清除文件信息", (source) => {
    vi.useFakeTimers();
    enterPriorStage();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    const agent = screen.getByRole("complementary", { name: "Tg Agent 面板" });
    expect(agent.querySelector(".ht-s1-upload-area.pending")).not.toBeNull();
    expect(within(agent).getByText("尚未上传先验数据")).not.toBeNull();
    expect(within(agent).getByText("上传 CSV 或使用样例")).not.toBeNull();
    expect(agent.querySelector(".ht-s1-file-name")).toBeNull();
    expect(within(agent).getByRole("status").textContent).toBe("未上传");

    if (source === "CSV") uploadSample("tg");
    else fireEvent.click(within(agent).getByRole("button", { name: "上传样例（Tg）" }));
    expect(agent.querySelector(".ht-s1-upload-area.loading")).not.toBeNull();
    expect(agent.querySelector(".ht-s1-file-name")?.textContent).toBe("doe_prior_tg.csv");
    expect(within(agent).getByText("正在准备先验数据")).not.toBeNull();
    expect(within(agent).getByRole("status").textContent).toBe("载入中…");
    expect(agent.textContent).not.toContain("已上传");
    expect(agent.textContent).not.toContain("12 条 DOE 样本");

    act(() => vi.advanceTimersByTime(1000));
    expect(agent.querySelector(".ht-s1-upload-area.ready")).not.toBeNull();
    expect(within(agent).getByText("已上传 · 12 条 DOE 样本")).not.toBeNull();
    expect(within(agent).getByRole("status").textContent).toBe("先验已就绪");
    expect(within(agent).queryByText("尚未上传先验数据")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重置 S1 上传数据" }));
    expect(agent.querySelector(".ht-s1-upload-area.pending")).not.toBeNull();
    expect(agent.querySelector(".ht-s1-file-name")).toBeNull();
    expect(within(agent).getByRole("status").textContent).toBe("未上传");
    expect(agent.textContent).not.toContain("已上传");
    expect(agent.textContent).not.toContain("12 条 DOE 样本");
  });

  it("上传样例无需选择文件，逐个载入对应先验并满足四份门禁", () => {
    vi.useFakeTimers();
    const view = enterPriorStage();
    for (const [index, target] of highThroughputDemoScenario.targets.entries()) {
      fireEvent.click(screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` }));
      const input = screen.getByLabelText(`上传 ${target.shortLabel} CSV 文件`);
      const picker = vi.spyOn(input, "click");
      const sample = screen.getByRole("button", { name: `上传样例（${target.shortLabel}）` });
      fireEvent.click(sample);
      expect(picker).not.toHaveBeenCalled();
      picker.mockRestore();
      expect(sample.hasAttribute("disabled")).toBe(true);
      expect(screen.getByRole("tab", { name: target.shortLabel }).getAttribute("aria-selected")).toBe("true");
      expect(screen.getByText("正在载入 DOE 样本")).not.toBeNull();
      act(() => vi.advanceTimersByTime(1000));
      expect(sample.hasAttribute("disabled")).toBe(false);
      const csv = highThroughputDemoScenario.doeCsvFiles[target.key];
      const table = screen.getByRole("table");
      expect(within(table).getAllByRole("row")).toHaveLength(13);
      expect(table.textContent).toContain(csv.rows[0].candidateId);
      expect(view.container.querySelectorAll(".ht-doe-sample")).toHaveLength(12);
      expect(view.container.querySelectorAll(".ht-s1-agent-status.ready")).toHaveLength(index + 1);
      const panel = screen.getByRole("complementary", { name: `${target.shortLabel} Agent 面板` });
      expect(panel.querySelector(".ht-s1-file-name")?.getAttribute("title")).toBe(csv.fileName);
    }
    expect(screen.getByRole("button", { name: "确认先验，进入 S2" }).hasAttribute("disabled")).toBe(false);
  });

  it("样例可修复上传错误，后续手动上传不会被未完成的样例覆盖", () => {
    vi.useFakeTimers();
    enterPriorStage();
    uploadSample("tg", "wrong.csv");
    expect(screen.getByRole("alert")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上传样例（Tg）" }));
    expect(screen.queryByRole("alert")).toBeNull();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText("先验已就绪")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上传样例（Tg）" }));
    act(() => vi.advanceTimersByTime(400));
    uploadSample("tg", "newer-file.csv");
    act(() => vi.advanceTimersByTime(1500));
    expect(screen.getByRole("alert").textContent).toContain("文件不匹配");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("先验已就绪")).toBeNull();
  });

  it("样例状态在收起后保留，重置和卸载都取消尚未完成的载入", () => {
    vi.useFakeTimers();
    const view = enterPriorStage();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    fireEvent.click(screen.getByRole("button", { name: "上传样例（Tg）" }));
    fireEvent.click(screen.getByRole("button", { name: "收起 Tg Agent" }));
    act(() => vi.advanceTimersByTime(1000));
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    expect(screen.getByText("先验已就绪")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上传样例（Tg）" }));
    fireEvent.click(screen.getByRole("button", { name: "重置 S1 上传数据" }));
    act(() => vi.advanceTimersByTime(1500));
    expect(screen.queryByText("先验已就绪")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上传样例（Tg）" }));
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("收起面板不丢失上传；重置取消加载，不会在超时后恢复旧结果", () => {
    vi.useFakeTimers();
    enterPriorStage();
    uploadSample("tg");
    fireEvent.click(screen.getByRole("button", { name: "收起 Tg Agent" }));
    act(() => vi.advanceTimersByTime(1000));
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    expect(screen.getByText("先验已就绪")).not.toBeNull();
    uploadSample("cte");
    fireEvent.click(screen.getByRole("button", { name: "重置 S1 上传数据" }));
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.queryByText("先验已就绪")).toBeNull();
    expect(screen.getAllByText("未上传")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "确认先验，进入 S2" }).hasAttribute("disabled")).toBe(true);
  });

  it("内部滚动壳兼容 S2–S6，保留原有确认门禁与完整演示路径", () => {
    vi.useFakeTimers();
    const view = enterPriorStage();
    const scrollRegion = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    for (const target of highThroughputDemoScenario.targets) uploadSample(target.key);
    act(() => vi.advanceTimersByTime(1000));
    fireEvent.click(screen.getByRole("button", { name: "确认先验，进入 S2" }));
    act(() => vi.advanceTimersByTime(900));

    const stage = () => view.container.querySelector('[aria-current="step"] span')?.textContent;
    const visited = new Set<string>();
    for (let step = 0; step < highThroughputDemoScenario.formulation.searchSteps.length + 6 && stage() !== "S6"; step += 1) {
      const previous = stage()!;
      visited.add(previous);
      expect(view.container.querySelector(".ht-scroll-region")).toBe(scrollRegion);
      expect(scrollRegion.querySelector(".ht-docx-board")).not.toBeNull();
      if (previous === "S2" || previous === "S3" || previous === "S4") {
        expect(view.container.querySelector(".ht-workbench-toolbar")).not.toBeNull();
        expect(view.container.querySelectorAll(".ht-property-grid-detail")).toHaveLength(1);
      } else {
        expect(view.container.querySelector(".ht-workbench-toolbar")).not.toBeNull();
        expect(view.container.querySelector(".ht-property-grid-detail")).toBeNull();
      }
      const confirm = screen.queryByRole("button", { name: previous === "S2" || previous === "S3" ? /^确认本批 [84] 项验证值$/ : /^确认本步 4 项验证值$/ });
      const next = previous === "S2" ? screen.getByRole<HTMLButtonElement>("button", { name: "进入 S3" })
        : previous === "S3" ? screen.getByRole<HTMLButtonElement>("button", { name: /^(进入 R2|进入收敛|进入 S4 候选输出)$/ })
        : previous === "S4" ? screen.getByRole<HTMLButtonElement>("button", { name: "进入 S5 多目标配比搜索" })
        : previous === "S5" ? view.container.querySelector<HTMLButtonElement>(".ht-s5-footer .ht-s1-primary-button")!
        : view.container.querySelector<HTMLButtonElement>(".ht-next-step-control")!;
      if (confirm) {
        expect(next.disabled).toBe(true);
        fireEvent.click(confirm);
      }
      expect(next.disabled).toBe(false);
      scrollRegion.scrollTop = 300;
      fireEvent.click(next);
      act(() => vi.advanceTimersByTime(1500));
      if (stage() !== previous) expect(scrollRegion.scrollTop).toBe(0);
    }
    expect([...visited]).toEqual(["S2", "S3", "S4", "S5"]);
    expect(stage()).toBe("S6");
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(view.container.querySelector(".ht-scroll-region")).toBe(scrollRegion);
  });
});
