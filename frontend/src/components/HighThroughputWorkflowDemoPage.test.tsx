/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { HighThroughputWorkflowDemoPage } from "./HighThroughputWorkflowDemoPage";

afterEach(() => {
  cleanup();
});

describe("HighThroughputWorkflowDemoPage S0", () => {
  it("将场景设置作为独立首阶段，并保留可交互的演示输入", () => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);

    expect(screen.getByRole("heading", { level: 1, name: "高通量优化演示" })).not.toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "材料体系与目标设置" })).not.toBeNull();
    expect(view.container.querySelector(".ht-s0-page")).not.toBeNull();
    const toolbar = view.container.querySelector(".ht-s0-module-toolbar");
    const board = view.container.querySelector(".ht-s0-board");
    expect(toolbar).not.toBeNull();
    expect(board).not.toBeNull();
    expect(toolbar?.nextElementSibling).toBe(board);
    expect(Array.from(board?.children ?? []).map((element) => element.className)).toEqual([
      "ht-s0-surface-header",
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

  it("可恢复默认参数，并通过场景确认进入原有 S1", async () => {
    const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
    const monomerACount = screen.getByRole("spinbutton", { name: "单体 A 候选数量" });

    fireEvent.change(monomerACount, { target: { value: "42" } });
    fireEvent.click(screen.getByRole("button", { name: "恢复默认场景参数" }));
    await waitFor(() => expect((monomerACount as HTMLInputElement).value).toBe("120"));

    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));

    expect(screen.queryByRole("heading", { level: 2, name: "材料体系与目标设置" })).toBeNull();
    expect(view.container.querySelector(".ht-s0-page")).toBeNull();
    expect(view.container.querySelector(".ht-s0-board")).toBeNull();
    expect(view.container.querySelector(".ht-s0-module-toolbar")).toBeNull();
    expect(screen.getByText("Tg Agent")).not.toBeNull();
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
