/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HighThroughputWorkflowDemoPage } from "../HighThroughputWorkflowDemoPage";
import { highThroughputDemoScenario as scenario, type HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { fallbackCandidateSmiles } from "./prior-hotspot-model";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

function loadPrior(key: HighThroughputTargetKey, fileName = scenario.doeCsvFiles[key].fileName) {
  const target = scenario.targets.find((item) => item.key === key)!;
  fireEvent.change(screen.getByLabelText(`上传 ${target.shortLabel} CSV 文件`), {
    target: { files: [new File(["fixed demo"], fileName, { type: "text/csv" })] },
  });
}

function enterS2(configure?: () => void) {
  vi.useFakeTimers();
  const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
  configure?.();
  fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
  scenario.targets.forEach((target) => loadPrior(target.key));
  act(() => vi.advanceTimersByTime(1000));
  fireEvent.click(screen.getByRole("tab", { name: "Tg" }));
  fireEvent.click(screen.getByRole("button", { name: "确认先验，进入 S2" }));
  act(() => vi.advanceTimersByTime(900));
  return view;
}

function next() { return screen.getByRole<HTMLButtonElement>("button", { name: "进入 S3" }); }
function confirm() { fireEvent.click(screen.getByRole("button", { name: "确认本批 8 项验证值" })); }
function input(id = "PI-1973", target = "Tg") { return screen.getByRole<HTMLInputElement>("spinbutton", { name: `${id} ${target} 演示验证值` }); }
function backToPrior() { fireEvent.click(screen.getByRole("button", { name: "返回 S1 先验导入" })); }
function returnToS2() {
  fireEvent.click(screen.getByRole("button", { name: "确认先验，进入 S2" }));
  act(() => vi.advanceTimersByTime(900));
}

// Each case imports all priors from S0/S1 and redraws the real candidate SVG.
describe("S2 先验热点与推荐验证", { timeout: 30000 }, () => {
  it("四目标的小数阈值在摘要、Agent 和验证区保持精度，展示与达标判断一致", () => {
    const thresholds = ["250.25", "27.75", "27.25", "2.91"];
    const view = enterS2(() => {
      scenario.targets.forEach((target, index) => {
        fireEvent.change(screen.getByRole("spinbutton", { name: `${target.shortLabel} 目标值` }), { target: { value: thresholds[index] } });
      });
    });
    scenario.targets.forEach((target, index) => {
      fireEvent.click(screen.getByRole("tab", { name: target.shortLabel }));
      const threshold = `${target.direction === "higher" ? "≥" : "≤"} ${thresholds[index]}`;
      expect(view.container.querySelector(".ht-s2-best-summary")?.textContent).toContain(`目标 ${threshold}`);
      expect(view.container.querySelector(".ht-s2-validation-threshold")?.textContent).toContain(`目标 ${threshold}`);
      expect(view.container.querySelector(".ht-s2-threshold-state")?.textContent).toBe("尚未达到场景阈值");
      const agent = view.container.querySelector(`[data-agent-theme="${target.key}"]`)!;
      expect(agent.querySelector(".ht-s1-agent-target")?.textContent).toContain(threshold);
      expect(agent.querySelector(".ht-s2-agent-analysis")?.textContent).toContain("尚未达到场景阈值");
    });
  });

  it("继承工作台框架、唯一滚动区和演示说明，默认收起四个 Agent", () => {
    const view = enterS2();
    expect(screen.getByRole("heading", { level: 1, name: "高通量优化演示" })).not.toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 2, name: "先验热点与推荐验证" }));
    expect(view.container.querySelector(".ht-s2-board.np-sw-accented-surface")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(view.container.querySelectorAll(".ht-flow-step")).toHaveLength(7);
    expect(view.container.querySelectorAll(".ht-agent-orbit, .ht-prior-workflow-grid, .ht-next-step-control")).toHaveLength(0);
    expect(screen.getByText(/不重训模型或改变预设搜索路径/)).not.toBeNull();
    expect(screen.getByRole("button", { name: "重置 S2 验证值" })).not.toBeNull();
    for (const target of scenario.targets) {
      expect(screen.getByRole("button", { name: `展开 ${target.shortLabel} Agent` }).getAttribute("aria-expanded")).toBe("false");
    }
    expect(view.container.querySelectorAll(".ht-s1-agent-flyout[hidden]")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: /上传/ })).toBeNull();
    expect(view.container.querySelectorAll('input[type="file"]')).toHaveLength(0);
    expect(screen.queryByRole("link", { name: /下载/ })).toBeNull();
    expect(view.container.querySelectorAll(".ht-doe-sample")).toHaveLength(12);
    expect(view.container.querySelectorAll(".ht-recommended-sample-node")).toHaveLength(2);
  });

  it("每个目标只有两个可编辑验证值，预填不代表确认；结构默认收起", () => {
    const view = enterS2();
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expect(input().value).not.toBe("");
    expect(next().disabled).toBe(true);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "确认本批 8 项验证值" }).disabled).toBe(false);
    expect(screen.getByText(/已预填示例值，可修改/)).not.toBeNull();
    expect(view.container.querySelector(".ht-s2-structure-fields")?.hasAttribute("hidden")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "展开结构详情" }));
    expect(view.container.querySelector(".ht-s2-structure-fields")?.hasAttribute("hidden")).toBe(false);
    expect(view.container.querySelectorAll(".ht-s2-structure-fields code")).toHaveLength(3);
    expect(view.container.querySelector("button input")).toBeNull();
    confirm();
    expect(next().disabled).toBe(false);
    expect(screen.getByText("本批已确认，可进入 S3")).not.toBeNull();
  });

  it("展开结构后通过卡片、图点、输入或目标切换仍保持展开，仅更新候选内容", () => {
    const view = enterS2();
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    scroll.scrollTo = vi.fn();
    fireEvent.click(screen.getByRole("button", { name: "展开结构详情" }));
    const fields = view.container.querySelector<HTMLElement>(".ht-s2-structure-fields")!;
    const controlsId = screen.getByRole("button", { name: "收起结构详情" }).getAttribute("aria-controls");

    function expectExpandedCandidate(candidateId: string) {
      const candidate = scenario.candidates.find((item) => item.id === candidateId)!;
      const smiles = fallbackCandidateSmiles(candidate);
      expect(view.container.querySelector(".ht-s2-structure-fields")).toBe(fields);
      expect(fields.hidden).toBe(false);
      expect(screen.getByRole("button", { name: "收起结构详情" }).getAttribute("aria-controls")).toBe(controlsId);
      expect(screen.getByRole("button", { name: "收起结构详情" }).getAttribute("aria-expanded")).toBe("true");
      expect(screen.getByRole("region", { name: `${candidateId} 候选详情` })).not.toBeNull();
      expect(Array.from(fields.querySelectorAll("code"), (element) => element.textContent)).toEqual([
        smiles.polymerSmiles, smiles.monomerASmiles, smiles.monomerBSmiles,
      ]);
    }

    fireEvent.click(screen.getByRole("button", { name: "查看 PI-699 推荐详情" }));
    expectExpandedCandidate("PI-699");
    fireEvent.keyDown(screen.getByRole("button", { name: "查看 PI-1973 推荐点并编辑 Tg 演示验证值" }), { key: "Enter" });
    expectExpandedCandidate("PI-1973");
    act(() => input("PI-699").focus());
    expectExpandedCandidate("PI-699");
    expect(document.activeElement).toBe(input("PI-699"));
    fireEvent.click(screen.getByRole("tab", { name: "CTE" }));
    expectExpandedCandidate("PI-1121");

    fireEvent.click(screen.getByRole("button", { name: "收起结构详情" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 PI-029 推荐详情" }));
    expect(view.container.querySelector(".ht-s2-structure-fields")).toBe(fields);
    expect(fields.hidden).toBe(true);
    expect(screen.getByRole("button", { name: "展开结构详情" }).getAttribute("aria-expanded")).toBe("false");
  });

  it("跨目标保留输入，任意缺项阻断整批，修改有效值也取消确认", () => {
    enterS2();
    confirm();
    fireEvent.change(input(), { target: { value: "281" } });
    expect(next().disabled).toBe(true);
    confirm();
    fireEvent.change(input("PI-699"), { target: { value: "" } });
    expect(screen.getByRole("alert").textContent).toContain("请输入有效数值");
    expect(input("PI-699").getAttribute("aria-invalid")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "CTE" }));
    expect(screen.getByText("待补 1 项：Tg 1 项")).not.toBeNull();
    expect(next().disabled).toBe(true);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "确认本批 8 项验证值" }).disabled).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "Tg" }));
    expect(input().value).toBe("281");
    expect(input("PI-699").value).toBe("");
    fireEvent.change(input("PI-699"), { target: { value: "279" } });
    confirm();
    expect(next().disabled).toBe(false);
  });

  it("图点、卡片和输入双向联动，键盘图点选择仅定位内部滚动区", () => {
    const view = enterS2();
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    const card = input("PI-699").closest<HTMLElement>(".ht-s2-validation-candidate")!;
    const scrollTo = vi.fn();
    scroll.scrollTo = scrollTo;
    scroll.scrollTop = 100;
    vi.spyOn(scroll, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 700 } as DOMRect);
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue({ top: 900, bottom: 1080 } as DOMRect);
    vi.spyOn(view.container.querySelector(".ht-s2-footer")!, "getBoundingClientRect").mockReturnValue({ top: 620 } as DOMRect);
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const mapPoint = screen.getByRole("button", { name: "查看 PI-699 推荐点并编辑 Tg 演示验证值" });
    fireEvent.keyDown(mapPoint, { key: "Enter" });
    expect(mapPoint.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "查看 PI-699 推荐详情" }).getAttribute("aria-pressed")).toBe("true");
    expect(scrollTo).toHaveBeenCalledWith({ top: 888, behavior: "auto" });
    expect(screen.getByRole("button", { name: "展开结构详情" }).getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "查看 PI-1973 推荐详情" }));
    expect(mapPoint.getAttribute("aria-pressed")).toBe("false");
    act(() => input("PI-699").focus());
    expect(document.activeElement).toBe(input("PI-699"));
    expect(mapPoint.getAttribute("aria-pressed")).toBe("true");
    scrollTo.mockClear();
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue({ top: 180, bottom: 400 } as DOMRect);
    fireEvent.keyDown(mapPoint, { key: " " });
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("目标页签键盘切换保持焦点与 tabpanel 关联", () => {
    enterS2();
    const tg = screen.getByRole("tab", { name: "Tg" });
    fireEvent.keyDown(tg, { key: "ArrowRight" });
    const cte = screen.getByRole("tab", { name: "CTE" });
    expect(document.activeElement).toBe(cte);
    expect(cte.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(cte.id);
    fireEvent.keyDown(cte, { key: "End" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Modulus" }));
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(document.activeElement).toBe(tg);
  });

  it("Agent 外层背景与状态可选中，两个内框不选中，且仅窄按钮收起", () => {
    const view = enterS2();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    const agent = screen.getByRole("complementary", { name: "Tg Agent 面板" });
    fireEvent.click(screen.getByRole("tab", { name: "CTE" }));
    for (const selector of [".ht-s1-agent-target", ".ht-s2-agent-analysis"]) {
      fireEvent.click(agent.querySelector(selector)!);
      expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
    }
    for (const selector of [".ht-s1-agent-card", ".ht-s1-agent-body", ".ht-s1-agent-status"]) {
      fireEvent.click(agent.querySelector(selector)!);
      expect(screen.getByRole("tab", { name: "Tg" }).getAttribute("aria-selected")).toBe("true");
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "查看 Tg Agent" }));
      fireEvent.click(screen.getByRole("tab", { name: "CTE" }));
    }
    expect(within(agent).getAllByRole("button")).toHaveLength(2);
    expect(agent.querySelector(".ht-s1-agent-target")?.textContent).toContain("°C");
    fireEvent.click(screen.getByRole("button", { name: "收起 Tg Agent" }));
    expect(view.container.querySelectorAll(".ht-s1-agent-flyout[hidden]")).toHaveLength(4);
  });

  it("返回 S1 保留验证值和批次确认，重新进入重置滚动并聚焦标题", () => {
    const view = enterS2();
    fireEvent.change(input(), { target: { value: "280" } });
    confirm();
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    scroll.scrollTop = 600;
    backToPrior();
    expect(scroll.scrollTop).toBe(0);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "正交实验与先验导入" }));
    expect(view.container.querySelectorAll(".ht-s1-upload-area.ready")).toHaveLength(4);
    scroll.scrollTop = 500;
    returnToS2();
    expect(scroll.scrollTop).toBe(0);
    expect(input().value).toBe("280");
    expect(next().disabled).toBe(false);
  });

  it.each(["valid", "invalid", "reset"])("返回后先验变更 %s 取消确认而保留输入", (action) => {
    enterS2();
    fireEvent.change(input(), { target: { value: "282" } });
    confirm();
    backToPrior();
    if (action === "reset") {
      fireEvent.click(screen.getByRole("button", { name: "重置 S1 上传数据" }));
      scenario.targets.forEach((target) => loadPrior(target.key));
    } else {
      loadPrior("tg", action === "invalid" ? "wrong.csv" : undefined);
      if (action === "invalid") {
        expect(screen.getByRole<HTMLButtonElement>("button", { name: "确认先验，进入 S2" }).disabled).toBe(true);
        loadPrior("tg");
      }
    }
    act(() => vi.advanceTimersByTime(1000));
    fireEvent.click(screen.getByRole("tab", { name: "Tg" }));
    returnToS2();
    expect(input().value).toBe("282");
    expect(next().disabled).toBe(true);
  });

  it("未改动 S0 不取消确认，实际修改阈值后取消且 S2 各区一致", () => {
    const view = enterS2();
    confirm();
    backToPrior();
    fireEvent.click(screen.getByRole("button", { name: "返回 S0 场景设置" }));
    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
    returnToS2();
    expect(next().disabled).toBe(false);
    backToPrior();
    fireEvent.click(screen.getByRole("button", { name: "返回 S0 场景设置" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Tg 目标值" }), { target: { value: "275" } });
    fireEvent.click(screen.getByRole("button", { name: "确认场景设置，进入 S1" }));
    returnToS2();
    expect(next().disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    for (const selector of [".ht-s1-agent-disclosure[data-agent-theme='tg'] .ht-s1-agent-target", ".ht-s2-best-summary", ".ht-s2-validation-threshold"]) {
      expect(view.container.querySelector(selector)?.textContent).toContain("275");
    }
  });

  it("S0 勾选不缩减固定四目标/8 项门禁，S2 重置仅恢复本批输入", () => {
    const view = enterS2(() => fireEvent.click(screen.getByRole("button", { name: "CTE" })));
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    const original = input().value;
    fireEvent.change(input(), { target: { value: "290" } });
    fireEvent.click(screen.getByRole("button", { name: "查看 PI-699 推荐详情" }));
    confirm();
    fireEvent.click(screen.getByRole("button", { name: "重置 S2 验证值" }));
    expect(input().value).toBe(original);
    expect(next().disabled).toBe(true);
    expect(screen.getByRole("button", { name: "查看 PI-1973 推荐详情" }).getAttribute("aria-pressed")).toBe("true");
    expect(view.container.querySelector(".ht-recommended-sample-node.selected")?.getAttribute("aria-label")).toContain("PI-1973");
    backToPrior();
    expect(view.container.querySelectorAll(".ht-s1-upload-area.ready")).toHaveLength(4);
  });

  it("推进期间禁用编辑、导航、确认与重置，但允许收起；卸载取消过渡", () => {
    const view = enterS2();
    fireEvent.click(screen.getByRole("button", { name: "展开 Tg Agent" }));
    confirm();
    const schedule = vi.spyOn(window, "setTimeout");
    const cancel = vi.spyOn(window, "clearTimeout");
    fireEvent.click(next());
    const transitionTimer = schedule.mock.results[schedule.mock.calls.findIndex((call) => call[1] === 1400)].value;
    expect(screen.getByText(/正在演示验证回流/)).not.toBeNull();
    expect(input().disabled).toBe(true);
    for (const name of ["返回 S1 先验导入", "重置 S2 验证值", "本批已确认", "演示回流中", "展开 CTE Agent"]) {
      expect(screen.getByRole<HTMLButtonElement>("button", { name }).disabled).toBe(true);
    }
    expect(screen.getAllByRole<HTMLButtonElement>("tab").every((tab) => tab.disabled)).toBe(true);
    expect(view.container.querySelector(".ht-recommended-sample-node")?.getAttribute("role")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "收起 Tg Agent" }));
    expect(screen.queryByRole("button", { name: "查看 Tg Agent" })).toBeNull();
    view.unmount();
    expect(cancel).toHaveBeenCalledWith(transitionTimer);
    act(() => vi.advanceTimersByTime(2000));
    expect(view.container.childElementCount).toBe(0);
  });
});
