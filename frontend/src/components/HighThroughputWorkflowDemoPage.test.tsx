/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { highThroughputDemoScenario, type HighThroughputTargetKey } from "../constants/highThroughputDemoScenario";
import { HighThroughputWorkflowDemoPage } from "./HighThroughputWorkflowDemoPage";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("HighThroughputWorkflowDemoPage S0", () => {
  it("将场景设置作为独立首阶段，并保留可交互的演示输入", () => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);

    expect(screen.getByRole("heading", { level: 1, name: "高通量优化演示" })).not.toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "材料体系与目标设置" })).not.toBeNull();
    expect(view.container.querySelector(".ht-s0-page")).not.toBeNull();
    const toolbar = view.container.querySelector(".ht-workbench-toolbar");
    const board = view.container.querySelector(".ht-s0-board");
    expect(toolbar).not.toBeNull();
    expect(board).not.toBeNull();
    expect(toolbar?.nextElementSibling).toBe(board);
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
    expect(screen.getAllByRole("link", { name: /下载 .* 样例 CSV/ })).toHaveLength(4);
    expect(board.querySelectorAll(".ht-s1-agent-flyout.np-sw-accented-surface")).toHaveLength(4);
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
      expect(panel.querySelector(".ht-s1-agent-id")?.textContent).toBe(`AGENT ${String(index + 1).padStart(2, "0")}`);
      expect(within(panel).getByText("当前查看")).not.toBeNull();
      expect(within(panel).getByText(target.direction === "higher" ? "越高越好" : "越低越好")).not.toBeNull();
      expect(panel.querySelector(".ht-s1-agent-target")?.textContent).toContain(target.unit === "degC" ? "°C" : target.unit);
      expect(within(panel).getByText("12 条 DOE 样本")).not.toBeNull();
      expect(within(panel).getByRole("status").textContent).toBe("等待导入");
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

  it("保留原有样例匹配与四份门禁，上传错误就地提示且可重试", () => {
    vi.useFakeTimers();
    const view = enterPriorStage();
    uploadSample("tg", "wrong-name.csv");
    expect(screen.getByRole("alert").textContent).toContain("文件不匹配");
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
    expect(screen.getByRole("button", { name: "重新上传 Tg CSV" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "正在生成先验热点" }).hasAttribute("disabled")).toBe(true);
    act(() => vi.advanceTimersByTime(900));
    expect(screen.getByText("S2", { selector: "[aria-current='step'] span" })).not.toBeNull();
    expect(view.container.querySelector(".ht-s1-page")).toBeNull();
    expect(view.container.querySelectorAll(".ht-agent-orbit")).toHaveLength(2);
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
    expect(screen.getAllByText("等待导入")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "确认先验，进入 S2" }).hasAttribute("disabled")).toBe(true);
  });
});
