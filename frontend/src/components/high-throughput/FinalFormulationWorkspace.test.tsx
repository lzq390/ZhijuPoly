/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { HighThroughputWorkflowDemoPage } from "../HighThroughputWorkflowDemoPage";
import { FinalFormulationWorkspace } from "./FinalFormulationWorkspace";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const tick = (ms = 1400) => act(() => vi.advanceTimersByTime(ms));
function renderFinal(overrides: Partial<ComponentProps<typeof FinalFormulationWorkspace>> = {}) {
  return render(<div className="ht-workbench-page"><main className="ht-scroll-region"><FinalFormulationWorkspace targets={scenario.targets} onBack={vi.fn()} onRestart={vi.fn()} {...overrides} /></main></div>);
}
function enterS6() {
  vi.useFakeTimers(); const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
  fireEvent.change(screen.getByLabelText("Tg 目标值"), { target: { value: "310" } });
  click("确认场景设置，进入 S1");
  for (const target of scenario.targets) { click(`展开 ${target.shortLabel} Agent`); click(`上传样例（${target.shortLabel}）`); }
  tick(1000); click("确认先验，进入 S2"); tick(900); click("确认本批 8 项验证值"); click("进入 S3"); tick();
  click("确认本批 8 项验证值"); click("进入 R2"); tick(); click("确认本批 4 项验证值"); click("进入收敛"); tick();
  click("进入 S4 候选输出"); tick(900); click("进入 S5 多目标配比搜索"); tick(900);
  click("开始邻域搜索"); tick(700);
  for (let step = 1; step <= 4; step++) {
    if (step === 4) fireEvent.change(screen.getByRole("spinbutton", { name: "mix-5 Tg 演示验证值" }), { target: { value: "999" } });
    click("确认本步 4 项验证值"); fireEvent.click(document.querySelector<HTMLButtonElement>(".ht-s5-footer .ht-s1-primary-button")!); tick(900);
  }
  return view;
}

describe("S6 最终解释工作台", () => {
  it("保留固定配方、综合分与完整演示披露，无输入或任务提交入口", () => {
    const view = renderFinal();
    expect(screen.getByText(/固定场景演示，结果为预设模拟数据/)).not.toBeNull();
    expect(screen.getByRole("heading", { name: "进入下一轮真实实验验证" })).not.toBeNull();
    expect(view.container.querySelector(".ht-s6-score")?.textContent).toContain("90 / 100");
    expect(view.container.querySelector(".ht-s6-composition")?.textContent).toBe("p140%p220%p320%p420%");
    expect(view.container.querySelectorAll("input,select,textarea")).toHaveLength(0);
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(view.container.querySelectorAll(".ht-s6-position,.ht-s6-radar-panel,.ht-s1-summary,.ht-s6-source-weight")).toHaveLength(0);
    expect(screen.queryByRole("img")).toBeNull();
    expect(view.container.textContent).not.toContain("目标权重");
    expect(view.container.textContent).not.toContain("结构展开状态随切换保留");
    expect(view.container.querySelectorAll(".ht-s6-records,table,summary")).toHaveLength(0);
    expect(view.container.textContent).not.toContain("验证记录对照");
    expect(view.container.textContent).not.toContain("S5 已确认验证值");
    expect(view.container.textContent).not.toContain("与预设一致");
  });
  it("按 S0 阈值展示达标情况与差值，摄氏度统一且不重算预设性质", () => {
    renderFinal({ targets: scenario.targets.map((target) => ({ ...target, target: target.key === "tg" ? 310.1 : target.target })) });
    const card = screen.getByRole("article", { name: "Tg 最终结果" });
    expect(card.querySelector(".ht-s6-property-value")?.textContent).toBe("292°C");
    expect(card.querySelector(".ht-s6-threshold")?.textContent).toContain("≥ 310.1 °C");
    expect(within(card).getByText("尚未达到场景阈值")).not.toBeNull();
    expect(document.querySelector(".ht-s6-result-status")?.textContent).toContain("3/4");
    expect(document.querySelector(".ht-s6-result-status")?.classList.contains("partial")).toBe(true);
    expect(document.querySelector(".ht-s6-margin")?.textContent).toContain("18.1");
    expect(document.querySelector(".ht-s6-stage")?.textContent).not.toContain("℃");
  });
  it("相等阈值明确显示恰好达标，不称为高于或低于", () => {
    renderFinal({ targets: scenario.targets.map((target) => ({ ...target, target: target.key === "tg" ? 292 : target.target })) });
    expect(screen.getByText("恰好达到场景阈值")).not.toBeNull();
    expect(screen.getByText("与阈值相等")).not.toBeNull();
    expect(document.querySelector(".ht-s6-result-status")?.textContent).toContain("4/4");
  });
  it("组分支持方向键、Home/End，切换保留结构展开并更新来源", () => {
    renderFinal(); click("展开结构详情");
    const first = screen.getByRole("tab", { name: "p1 Tg 来源" }); first.focus(); fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "p2 CTE 来源" }));
    expect(screen.getByRole("button", { name: "收起结构详情" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("tabpanel").textContent).toContain("PI-1219");
    fireEvent.keyDown(document.activeElement!, { key: "End" });
    expect(screen.getByRole("tabpanel").textContent).toContain("PI-356");
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(screen.getByRole("tabpanel").textContent).toContain("PI-1013");
    expect(screen.getByRole("tabpanel").querySelectorAll("code")).toHaveLength(3);
    expect(document.querySelector(".ht-s6-source-caption")?.getAttribute("aria-live")).toBe("polite");
    expect(screen.getByRole("tabpanel").textContent).toContain("Tg Agent · S3 收敛预设输出");
  });
  it("底栏预留空间在卸载时清除，返回和重启操作各自独立", () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(126);
    const onBack = vi.fn(), onRestart = vi.fn(); const view = renderFinal({ onBack, onRestart });
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    expect(scroll.style.getPropertyValue("--ht-s6-footer-height")).toBe("126px");
    click("返回 S5 配比搜索"); expect(onBack).toHaveBeenCalledOnce();
    click("重新开始"); expect(onRestart).toHaveBeenCalledOnce();
    view.unmount(); expect(scroll.style.getPropertyValue("--ht-s6-footer-height")).toBe("");
  });
  it("S5→S6 外框连续、返回续接确认值，再进入仍是预设结果", () => {
    const view = enterS6();
    expect(view.container.querySelector(".ht-s6-board.np-sw-accented-surface")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "最终配方与结果解释" }));
    expect(view.container.querySelectorAll(".ht-agent-orbit, .ht-next-step-control, .ht-final-formulation-panel")).toHaveLength(0);
    expect(view.container.querySelector(".ht-workbench-toolbar")).not.toBeNull();
    expect(view.container.querySelector(".ht-workbench-toolbar button")).toBeNull();
    expect(view.container.querySelector(".ht-s6-stage")?.textContent).not.toContain("999");
    click("展开结构详情"); click("返回 S5 配比搜索");
    expect(document.querySelector(".ht-s5-step-nav [aria-pressed=true]")?.getAttribute("data-step")).toBe("4");
    expect(screen.getByRole<HTMLInputElement>("spinbutton", { name: "mix-5 Tg 演示验证值" }).value).toBe("999");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "进入 S6 最终解释" }).disabled).toBe(false);
    fireEvent.change(screen.getByRole("spinbutton", { name: "mix-5 Tg 演示验证值" }), { target: { value: "1000" } });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "进入 S6 最终解释" }).disabled).toBe(true);
    click("确认本步 4 项验证值");
    click("进入 S6 最终解释"); tick(900);
    expect(screen.getByRole("button", { name: "展开结构详情" }).getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector(".ht-s6-property-value")?.textContent).toBe("292°C");
    expect(document.querySelector(".ht-s6-score")?.textContent).toContain("90 / 100");
    expect(view.container.querySelector(".ht-s6-stage")?.textContent).not.toContain("1000");
    expect(screen.queryByText("验证记录对照")).toBeNull();
  });
  it("重新开始二次确认：取消保留，确认清全会话并回到默认 S0", () => {
    const view = enterS6(); const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    click("展开结构详情"); fireEvent.click(screen.getByRole("tab", { name: "p2 CTE 来源" }));
    click("重新开始"); expect(view.container.querySelector(".ht-s6-stage")).not.toBeNull();
    expect(screen.getByRole("tab", { name: "p2 CTE 来源" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("button", { name: "收起结构详情" }).getAttribute("aria-expanded")).toBe("true");
    confirm.mockReturnValue(true); click("重新开始");
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("清除 S0–S6"));
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "材料体系与目标设置" }));
    expect(screen.getByRole<HTMLInputElement>("spinbutton", { name: "Tg 目标值" }).value).toBe("250");
    click("确认场景设置，进入 S1"); tick(5000);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "确认先验，进入 S2" }).disabled).toBe(true);
    for (const target of scenario.targets) { click(`展开 ${target.shortLabel} Agent`); click(`上传样例（${target.shortLabel}）`); }
    tick(1000); click("确认先验，进入 S2"); tick(900);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "进入 S3" }).disabled).toBe(true);
    click("确认本批 8 项验证值"); click("进入 S3"); tick();
    expect(document.querySelector(".ht-s3-stage")?.getAttribute("data-view-round")).toBe("0");
    click("确认本批 8 项验证值"); click("进入 R2"); tick(); click("确认本批 4 项验证值"); click("进入收敛"); tick();
    click("进入 S4 候选输出"); tick(900); click("进入 S5 多目标配比搜索"); tick(900);
    expect(document.querySelector(".ht-s5-step-nav [aria-pressed=true]")?.getAttribute("data-step")).toBe("0");
    click("开始邻域搜索"); tick(700);
    for (let step = 1; step < 4; step++) { click("确认本步 4 项验证值"); fireEvent.click(document.querySelector<HTMLButtonElement>(".ht-s5-footer .ht-s1-primary-button")!); tick(700); }
    expect(screen.getByRole<HTMLInputElement>("spinbutton", { name: "mix-5 Tg 演示验证值" }).value).toBe("292");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "进入 S6 最终解释" }).disabled).toBe(true);
  }, 15000); // Walk the full workflow twice to verify reset, including under parallel suite load.

  it("配方占比、性质值的数字与单位分离，复用数值和正文字体", () => {
    const view = renderFinal();
    const tokens = (selector: string) => [...view.container.querySelectorAll(selector)].map((element) => element.textContent);
    expect(tokens(".ht-s6-source-ratio b")).toEqual(["40", "20", "20", "20"]);
    expect(tokens(".ht-s6-source-ratio > span > span")).toEqual(["%", "%", "%", "%"]);
    expect(tokens(".ht-s6-property-value b")).toEqual(["292", "29", "18", "3.3"]);
    expect(tokens(".ht-s6-property-value > span")).toEqual(["°C", "ppm/K", "%", "GPa"]);
    expect(tokens(".ht-s6-stage b").every((text) => !/[%\u4e00-\u9fff]/.test(text ?? ""))).toBe(true);
  });
});
