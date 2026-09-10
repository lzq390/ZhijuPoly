/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { HighThroughputWorkflowDemoPage } from "../HighThroughputWorkflowDemoPage";
import { fallbackCandidateSmiles, getDemoCandidate } from "./prior-hotspot-model";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
const button = (name: string | RegExp) => screen.getByRole<HTMLButtonElement>("button", { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
const tick = (ms = 1400) => act(() => vi.advanceTimersByTime(ms));
const tab = (name: string) => screen.getByRole<HTMLButtonElement>("tab", { name });
const activeId = () => document.querySelector(".ht-s4-candidate-heading strong")!.textContent;

function enterS4(configure?: () => void, editR1?: () => void) {
  vi.useFakeTimers();
  const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
  configure?.();
  click("确认场景设置，进入 S1");
  for (const target of scenario.targets) { click(`展开 ${target.shortLabel} Agent`); click(`上传样例（${target.shortLabel}）`); }
  tick(1000);
  fireEvent.click(tab("Tg"));
  click("确认先验，进入 S2"); tick(900);
  click("确认本批 8 项验证值"); click("进入 S3"); tick();
  editR1?.();
  click("确认本批 8 项验证值"); click("进入 R2"); tick();
  click("确认本批 4 项验证值"); click("进入收敛"); tick();
  click("进入 S4 候选输出"); tick(900);
  const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
  scroll.scrollTo = vi.fn();
  return { ...view, scroll };
}

describe("S4 候选输出工作台", () => {
  it("继承统一外框、固定披露和七阶段导航，无输入、确认门禁或无效重置", () => {
    const view = enterS4();
    expect(view.container.querySelector(".ht-s4-board.np-sw-accented-surface")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 2, name: "单性质候选输出" }));
    expect(screen.getByRole("heading", { level: 1, name: "高通量优化演示" })).not.toBeNull();
    expect(screen.getByText(/S3 验证值仅用于回流记录对照/)).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-agent-orbit, .ht-next-step-control, .ht-candidate-output-panel, input")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /重置|重演|确认本批/ })).toBeNull();
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    for (const component of scenario.formulation.components) {
      const label = scenario.targets.find((target) => target.key === component.sourceTargetKey)!.shortLabel;
      expect(within(tab(`${component.id} ${label} 预设输出`)).getByText(component.candidateId)).not.toBeNull();
    }
    expect(view.container.querySelectorAll(".ht-s4-property")).toHaveLength(4);
    expect(view.container.querySelectorAll(".ht-s4-map-point")).toHaveLength(3);
    expect(view.container.querySelectorAll(".ht-doe-sample, .ht-tested-sample, .ht-recommended-sample-node")).toHaveLength(0);
    expect(button("进入 S5 多目标配比搜索").disabled).toBe(false);
    expect(view.container.querySelector(".ht-flow-steps [aria-current='step']")?.textContent).toContain("S4");
  });

  it("组分切换支持方向键、Home/End，详情、备选及位置图同步目标", () => {
    enterS4();
    fireEvent.keyDown(tab("p1 Tg 预设输出"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab("p2 CTE 预设输出"));
    expect(activeId()).toBe("PI-1219");
    expect(screen.getByRole("group", { name: "CTE 固定输出与备选位置图" })).not.toBeNull();
    expect(button("查看 PI-1121 备选")).not.toBeNull();
    fireEvent.keyDown(tab("p2 CTE 预设输出"), { key: "End" });
    expect(tab("p4 Modulus 预设输出").getAttribute("aria-selected")).toBe("true");
    expect(activeId()).toBe("PI-356");
    fireEvent.keyDown(tab("p4 Modulus 预设输出"), { key: "ArrowRight" });
    expect(activeId()).toBe("PI-1013");
    fireEvent.keyDown(tab("p1 Tg 预设输出"), { key: "ArrowLeft" });
    fireEvent.keyDown(tab("p4 Modulus 预设输出"), { key: "Home" });
    expect(activeId()).toBe("PI-1013");
  });

  it("结构详情独立位于性质与位置区之后，展开与收起不移动或重建候选区", () => {
    const view = enterS4();
    const layout = view.container.querySelector(".ht-s4-detail-layout")!;
    const main = layout.querySelector(".ht-s4-detail-main")!;
    const position = layout.querySelector(".ht-s4-position-panel")!;
    const structure = layout.querySelector(".ht-s4-structure-slot")!;
    expect([...layout.children]).toEqual([main, position, structure]);
    expect(main.querySelector(".ht-s2-structure-detail")).toBeNull();
    const fields = structure.querySelector<HTMLElement>(".ht-s2-structure-fields")!;
    expect(fields.hidden).toBe(true);
    click("展开结构详情");
    expect(fields.hidden).toBe(false);
    expect(fields.querySelectorAll("dl > div")).toHaveLength(3);
    expect([...fields.querySelectorAll("dt")].map((label) => label.textContent)).toEqual(["Polymer SMILES", "单体 A SMILES", "单体 B SMILES"]);
    click("收起结构详情");
    expect(fields.hidden).toBe(true);
    expect([...layout.children]).toEqual([main, position, structure]);
  });

  it("备选与图点双向联动，结构展开跨候选和目标保留，固定输出不被替换", () => {
    const view = enterS4();
    click("展开结构详情");
    const fields = view.container.querySelector<HTMLElement>(".ht-s2-structure-fields")!;
    click("查看 PI-2842 备选");
    expect(activeId()).toBe("PI-2842");
    expect(fields.hidden).toBe(false);
    expect(fields.textContent).toContain(fallbackCandidateSmiles(getDemoCandidate("PI-2842")).polymerSmiles);
    expect(button("在位置图查看 PI-2842 备选").getAttribute("aria-pressed")).toBe("true");
    expect(view.container.querySelector(".ht-current-best-marker")?.getAttribute("data-candidate-id")).toBe("PI-1013");
    fireEvent.keyDown(button("在位置图查看 PI-2326 备选"), { key: " " });
    expect(button("查看 PI-2326 备选").getAttribute("aria-pressed")).toBe("true");
    expect(activeId()).toBe("PI-2326");
    fireEvent.keyDown(button("在位置图查看 PI-1013 固定输出"), { key: "Enter" });
    expect(activeId()).toBe("PI-1013");
    click("查看 PI-2842 备选");
    fireEvent.click(tab("p2 CTE 预设输出"));
    expect(fields.hidden).toBe(false);
    expect(activeId()).toBe("PI-1219");
    fireEvent.click(tab("p1 Tg 预设输出"));
    expect(activeId()).toBe("PI-2842");
    expect(fields.hidden).toBe(false);
  });

  it("阈值统一读取 S0，小数不丢失、CTE 向下比较，取消勾选也不删固定组分", () => {
    const view = enterS4(() => {
      fireEvent.change(screen.getByRole("spinbutton", { name: "Tg 目标值" }), { target: { value: "300.25" } });
      fireEvent.change(screen.getByRole("spinbutton", { name: "CTE 目标值" }), { target: { value: "24.5" } });
      fireEvent.click(button("Elongation"));
    });
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(view.container.querySelector('[data-property="tg"] .ht-s4-property-threshold')?.textContent).toBe("目标 ≥ 300.25 °C");
    expect(view.container.querySelector('[data-property="tg"] .ht-s4-attainment')?.textContent).toBe("尚未达到场景阈值");
    fireEvent.click(tab("p2 CTE 预设输出"));
    expect(view.container.querySelector('[data-property="cte"] .ht-s4-property-threshold')?.textContent).toBe("目标 ≤ 24.5 ppm/K");
    expect(view.container.querySelector('[data-property="cte"] .ht-s4-attainment')?.textContent).toBe("尚未达到场景阈值");
    expect(view.container.querySelector(".ht-s4-stage")?.textContent).not.toContain("degC");
  });

  it("回流极值仅在来源对照中生效，返回 S3 保留收敛进度、批次值和确认", () => {
    const view = enterS4(undefined, () => fireEvent.change(screen.getByRole("spinbutton", { name: "PI-2326 Tg 演示验证值" }), { target: { value: "999.25" } }));
    expect(view.container.querySelector(".ht-s4-provenance")?.textContent).toContain("999.25");
    expect(activeId()).toBe("PI-1013");
    expect(view.container.querySelector('[data-property="tg"] .ht-s4-property-value')?.textContent).toContain("296");
    click("返回 S3 收敛对照");
    expect(view.container.querySelector<HTMLElement>(".ht-s3-stage")?.dataset.progressRound).toBe("2");
    expect(view.container.querySelector<HTMLElement>(".ht-s3-stage")?.dataset.viewRound).toBe("2");
    click("R1 已完成");
    expect(screen.getByLabelText("PI-2326 Tg 已确认演示验证值").textContent).toContain("999.25");
    click("返回当前轮次");
    click("进入 S4 候选输出"); tick(900);
    expect(activeId()).toBe("PI-1013");
    expect(button("进入 S5 多目标配比搜索").disabled).toBe(false);
  });

  it("图点选择只定位内部详情区，不滚动整个页面", () => {
    const view = enterS4();
    const pageScroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    vi.spyOn(view.scroll, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 800 } as DOMRect);
    vi.spyOn(view.container.querySelector(".ht-s4-candidate-focus")!, "getBoundingClientRect").mockReturnValue({ top: 900, bottom: 1200 } as DOMRect);
    click("在位置图查看 PI-2842 备选");
    expect(view.scroll.scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: 788 }));
    expect(pageScroll).not.toHaveBeenCalled();
  });

  it("推进锁定所有查看与返回动作，选择备选不改变 S5 组分池", () => {
    const view = enterS4();
    click("查看 PI-2842 备选");
    click("进入 S5 多目标配比搜索");
    expect(view.container.querySelector(".ht-s4-stage")?.getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByRole<HTMLButtonElement>("tab").every((item) => item.disabled)).toBe(true);
    expect(button("返回 S3 收敛对照").disabled).toBe(true);
    expect(button("展开结构详情").disabled).toBe(true);
    expect(button("在位置图查看 PI-1013 固定输出").getAttribute("aria-disabled")).toBe("true");
    click("在位置图查看 PI-1013 固定输出");
    expect(activeId()).toBe("PI-2842");
    tick(900);
    expect(view.container.querySelector(".ht-s4-stage")).toBeNull();
    expect(view.container.querySelector(".ht-s5-stage")).not.toBeNull();
    const pool = view.container.querySelector(".ht-s5-components");
    for (const component of scenario.formulation.components) expect(pool?.textContent).toContain(component.candidateId);
    expect(view.scroll.style.getPropertyValue("--ht-s4-footer-height")).toBe("");
    expect(view.scroll.scrollTop).toBe(0);
  });

  it("窄屏进入时只将阶段条水平定位到 S4，返回时恢复上游阶段条位置", () => {
    const original = HTMLElement.prototype.getBoundingClientRect;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains("ht-flow-steps")) return { left: 10, right: 320, width: 310 } as DOMRect;
      if (this.classList.contains("ht-flow-step") && this.textContent?.startsWith("S4")) return { left: 410, right: 502, width: 92 } as DOMRect;
      return original.call(this);
    });
    vi.spyOn(Element.prototype, "scrollWidth", "get").mockImplementation(function (this: Element) { return this.classList.contains("ht-flow-steps") ? 700 : 0; });
    vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function (this: Element) { return this.classList.contains("ht-flow-steps") ? 310 : 0; });
    const view = enterS4();
    const nav = view.container.querySelector<HTMLElement>(".ht-flow-steps")!;
    expect(nav.scrollLeft).toBe(291);
    expect(view.scroll.scrollTop).toBe(0);
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 2, name: "单性质候选输出" }));
    click("返回 S3 收敛对照");
    expect(nav.scrollLeft).toBe(0);
  });
});
