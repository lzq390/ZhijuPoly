/* @vitest-environment jsdom */
import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { HighThroughputWorkflowDemoPage } from "../HighThroughputWorkflowDemoPage";
import { RatioSearchWorkspace } from "./RatioSearchWorkspace";
import { buildInitialRatioValidationValues, buildInitialRatioValidationConfirmations, ratioValidationMissingCount } from "./ratio-search-model";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
const button = (name: string | RegExp) => screen.getByRole<HTMLButtonElement>("button", { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
const tick = (ms = 1400) => act(() => vi.advanceTimersByTime(ms));
const next = () => fireEvent.click(document.querySelector<HTMLButtonElement>(".ht-s5-footer .ht-s1-primary-button")!);
const field = (id = "mix-1", target = "Tg") => screen.getByRole<HTMLInputElement>("spinbutton", { name: `${id} ${target} 演示验证值` });
const viewedStep = () => document.querySelector(".ht-s5-step-nav [aria-pressed='true']")?.getAttribute("data-step");

function Harness({ initialStep = 0, busy = false }: { initialStep?: number; busy?: boolean }) {
  const [progress, setProgress] = useState(initialStep);
  const [view, setView] = useState(initialStep);
  const [values, setValues] = useState(buildInitialRatioValidationValues);
  const [confirmations, setConfirmations] = useState(() => ({ ...buildInitialRatioValidationConfirmations(),
    ...Object.fromEntries(scenario.formulation.searchSteps.slice(0, initialStep).map((step) => [step.proposedMixId, true])) }));
  const mixId = scenario.formulation.searchSteps[progress].proposedMixId;
  return <div className="ht-workbench-page"><main className="ht-scroll-region"><RatioSearchWorkspace targets={scenario.targets} progressStep={progress} viewStep={view} onViewStep={setView}
    validationValues={values} confirmations={confirmations} onValidationValueChange={(key, value) => { setValues((all) => ({ ...all, [mixId]: { ...all[mixId], [key]: value } })); setConfirmations((all) => ({ ...all, [mixId]: false })); }}
    onConfirm={() => setConfirmations((all) => ({ ...all, [mixId]: true }))} onBack={vi.fn()} onNext={() => { setProgress(progress + 1); setView(progress + 1); }}
    canAdvance={progress === 0 || Boolean(confirmations[mixId]) && ratioValidationMissingCount(mixId, values) === 0} transitionMessage={busy ? "正在演示验证回流" : null} /></main></div>;
}

function enterS5() {
  vi.useFakeTimers(); const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
  // The S0 threshold must appear everywhere in S5, not the scene's baked-in target.
  fireEvent.change(screen.getByLabelText("Tg 目标值"), { target: { value: "310" } });
  click("确认场景设置，进入 S1");
  for (const target of scenario.targets) { click(`展开 ${target.shortLabel} Agent`); click(`上传样例（${target.shortLabel}）`); }
  tick(1000); click("确认先验，进入 S2"); tick(900);
  click("确认本批 8 项验证值"); click("进入 S3"); tick();
  click("确认本批 8 项验证值"); click("进入 R2"); tick();
  click("确认本批 4 项验证值"); click("进入收敛"); tick();
  click("进入 S4 候选输出"); tick(900); click("进入 S5 多目标配比搜索"); tick(900);
  return view;
}

describe("S5 配比搜索工作台", () => {
  it("起点无伪输入，固定组分、四目标权重与图表说明完整", () => {
    const view = render(<Harness />);
    expect(screen.queryByRole("spinbutton")).toBeNull(); expect(button("开始邻域搜索").disabled).toBe(false);
    expect(screen.getByText(/不重算得分、接受概率或决策/)).not.toBeNull();
    const pool = view.container.querySelector(".ht-s5-components")!;
    for (const component of scenario.formulation.components) expect(pool.textContent).toContain(component.candidateId);
    expect(within(pool as HTMLElement).getByText("30%")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-s5-map-grid-point")).toHaveLength(286);
    expect(screen.getByRole("img", { name: /T0 配比搜索投影/ })).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-s5-step-nav button:disabled")).toHaveLength(4);
  });
  it("预填四项仍需确认；修改取消确认，空值就地报错并阻断", () => {
    render(<Harness initialStep={1} />);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(4);
    expect(button("进入 T2 继续爬升").disabled).toBe(true);
    click("确认本步 4 项验证值"); expect(button("进入 T2 继续爬升").disabled).toBe(false);
    fireEvent.change(field(), { target: { value: "" } });
    expect(field().getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe("请输入有限数值");
    expect(button("确认本步 4 项验证值").disabled).toBe(true); expect(button("进入 T2 继续爬升").disabled).toBe(true);
    fireEvent.change(field(), { target: { value: "999" } });
    expect(button("确认本步 4 项验证值").disabled).toBe(false); expect(button("进入 T2 继续爬升").disabled).toBe(true);
    expect(screen.getByLabelText("T1 预设决策").textContent).toContain("待确认后展示");
    click("确认本步 4 项验证值"); next(); expect(viewedStep()).toBe("2");
    click("查看 T1 高温接受"); expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(document.querySelector(".ht-s5-readonly-value")?.textContent).toContain("999");
  });
  it("摘要数据与中文单位分离，回看保留完整长数值", () => {
    const view = render(<Harness initialStep={1} />);
    const tokens = (selector: string) => [...view.container.querySelectorAll(`${selector} b`)].map((element) => element.textContent);
    expect(tokens(".ht-s1-summary .ht-s5-status")).toEqual(["T1", "4/4"]);
    expect(tokens(".ht-s5-map-metrics small")).toEqual(["5", "66"]);
    click("确认本步 4 项验证值");
    expect(tokens(".ht-s1-summary .ht-s5-status")).toEqual(["T1", "4"]);
    fireEvent.change(field(), { target: { value: "" } });
    expect(tokens(".ht-s1-summary .ht-s5-status")).toEqual(["T1", "1"]);
    fireEvent.change(field(), { target: { value: "1e24" } });
    click("确认本步 4 项验证值"); next(); click("查看 T1 高温接受");
    expect(view.container.querySelector(".ht-s5-readonly-value b")?.textContent).toBe((1e24).toLocaleString("en-US"));
    expect(view.container.querySelector(".ht-s5-readonly-value > span")?.textContent).toBe("°C");
    expect(tokens(".ht-s5-status").every((text) => !/[\u4e00-\u9fff]/.test(text ?? ""))).toBe(true);
  });
  it("已完成步骤只读回看、未来禁用，键盘切换后可返回当前步骤", () => {
    render(<Harness initialStep={3} />);
    expect(button("查看 T4 锁定候选").disabled).toBe(true);
    fireEvent.keyDown(button("查看 T3 低温拒绝"), { key: "Home" });
    expect(viewedStep()).toBe("0"); expect(document.activeElement).toBe(button("查看 T0 初始化"));
    expect(screen.queryByRole("button", { name: "返回 S4 候选输出" })).toBeNull();
    expect(screen.queryByRole("button", { name: "确认本步 4 项验证值" })).toBeNull();
    expect(document.querySelector(".ht-s5-step-nav [aria-current='step']")?.getAttribute("data-step")).toBe("3");
    fireEvent.keyDown(button("查看 T0 初始化"), { key: "ArrowDown" }); expect(viewedStep()).toBe("1");
    expect(screen.queryByRole("spinbutton")).toBeNull(); expect(document.querySelectorAll(".ht-s5-readonly-value")).toHaveLength(4);
    expect(screen.getByRole("img", { name: /T1 配比搜索投影/ }).getAttribute("aria-label")).not.toContain("mix-6");
    click("返回当前步骤"); expect(viewedStep()).toBe("3"); expect(screen.getAllByRole("spinbutton")).toHaveLength(4);
  });
  it("确认拒绝后保留 mix-4；温度、分差、概率均明确为预设", () => {
    render(<Harness initialStep={3} />); click("确认本步 4 项验证值");
    expect(screen.getByLabelText("决策后保留 mix-4")).not.toBeNull();
    const decision = screen.getByLabelText("T3 预设决策");
    expect(decision.textContent).toContain("拒绝邻域解"); expect(decision.textContent).toContain("16%");
    expect(screen.getByRole("img", { name: /T3 配比搜索投影/ }).getAttribute("aria-label")).toContain("mix-6 预设拒绝");
  });
  it("过渡锁定输入、步骤、确认、返回和推进", () => {
    const view = render(<Harness initialStep={2} busy />);
    expect(view.container.querySelector(".ht-s5-stage")?.getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByRole<HTMLInputElement>("spinbutton").every((input) => input.disabled)).toBe(true);
    expect(screen.getAllByRole<HTMLButtonElement>("button").every((item) => item.disabled)).toBe(true);
  });
  it("步骤变化只定位内部图区与步骤焦点，离开后清除底栏预留", () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) { return this.classList.contains("ht-s5-footer") ? 144 : 0; });
    const bounds = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect");
    bounds.mockImplementation(function (this: HTMLElement) {
      return { top: this.classList.contains("ht-s5-search-panel") ? -100 : 0, left: 0, right: 100, bottom: 100, width: 100, height: 100 } as DOMRect;
    });
    const view = render(<Harness initialStep={1} />);
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    expect(scroll.style.getPropertyValue("--ht-s5-footer-height")).toBe("144px");
    scroll.scrollTop = 300;
    click("确认本步 4 项验证值"); next();
    expect(scroll.scrollTop).toBe(188);
    expect(document.activeElement).toBe(button("查看 T2 继续爬升"));
    expect(document.documentElement.scrollTop).toBe(0);
    view.unmount(); expect(scroll.style.getPropertyValue("--ht-s5-footer-height")).toBe("");
  });
  // Only full-page replay tests need CI headroom; isolated S5 cases keep the default timeout.
  it("S4→S5 外框连续，返回续接；重演取消保留、确认恢复默认且不清先验", { timeout: 30000 }, () => {
    const view = enterS5();
    expect(view.container.querySelector(".ht-s5-board.np-sw-accented-surface")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "多目标配比搜索" }));
    expect(view.container.querySelectorAll(".ht-agent-orbit, .ht-next-step-control, .ht-ratio-search-panel")).toHaveLength(0);
    click("开始邻域搜索"); tick(700);
    expect(document.querySelector(".ht-s5-field-threshold")?.textContent).toContain("310");
    expect(document.querySelector(".ht-s5-field-threshold")?.textContent).toContain("°C");
    fireEvent.change(field(), { target: { value: "500" } }); click("确认本步 4 项验证值");
    click("返回 S4 候选输出"); click("进入 S5 多目标配比搜索"); tick(900);
    expect(viewedStep()).toBe("1"); expect(field().value).toBe("500"); expect(button("进入 T2 继续爬升").disabled).toBe(false);
    next(); expect(button("重演 S5").disabled).toBe(true); tick(700);
    click("查看 T1 高温接受"); expect(screen.queryByRole("button", { name: "重演 S5" })).toBeNull(); click("返回当前步骤");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    click("重演 S5"); expect(viewedStep()).toBe("2");
    confirm.mockReturnValue(true); click("重演 S5"); expect(viewedStep()).toBe("0");
    click("开始邻域搜索"); tick(700); expect(field().value).toBe(buildInitialRatioValidationValues()["mix-1"].tg);
    expect(button("进入 T2 继续爬升").disabled).toBe(true);
    click("返回 S4 候选输出"); click("返回 S3 收敛对照"); click("返回 S2 推荐验证"); click("返回 S1 先验导入");
    expect(button("确认先验，进入 S2").disabled).toBe(false);
  });
  it("上游 S2 编辑撤销 S5 进度与确认，但保留各步已填值", { timeout: 30000 }, () => {
    enterS5(); click("开始邻域搜索"); tick(700); fireEvent.change(field(), { target: { value: "501" } }); click("确认本步 4 项验证值"); next(); tick(700);
    click("返回 S4 候选输出"); click("返回 S3 收敛对照"); click("返回 S2 推荐验证");
    fireEvent.change(screen.getAllByRole("spinbutton")[0], { target: { value: "555" } });
    click("确认本批 8 项验证值"); click("进入 S3"); tick();
    click("确认本批 8 项验证值"); click("进入 R2"); tick(); click("确认本批 4 项验证值"); click("进入收敛"); tick();
    click("进入 S4 候选输出"); tick(900); click("进入 S5 多目标配比搜索"); tick(900);
    expect(viewedStep()).toBe("0"); click("开始邻域搜索"); tick(700); expect(field().value).toBe("501"); expect(button("进入 T2 继续爬升").disabled).toBe(true);
  });
  it.each(["S0 参数不变", "S0 参数变化", "S1 上传失败", "S3 重演"])("%s 正确管理 S5 进度与历史输入", (action) => {
    enterS5(); click("开始邻域搜索"); tick(700); fireEvent.change(field(), { target: { value: "502" } }); click("确认本步 4 项验证值"); next(); tick(700);
    click("返回 S4 候选输出"); click("返回 S3 收敛对照");
    if (action === "S3 重演") {
      vi.spyOn(window, "confirm").mockReturnValue(true); click("重演 S3");
    } else {
      click("返回 S2 推荐验证"); click("返回 S1 先验导入");
      if (action === "S1 上传失败") {
        click("展开 Tg Agent"); fireEvent.change(document.querySelector<HTMLInputElement>('input[type="file"]')!, { target: { files: [new File(["bad"], "bad.csv")] } });
        expect(button("确认先验，进入 S2").disabled).toBe(true);
        click("上传样例（Tg）"); tick(1000);
      } else {
        click("返回 S0 场景设置");
        if (action === "S0 参数变化") fireEvent.change(screen.getByLabelText("Tg 目标值"), { target: { value: "311" } });
        click("确认场景设置，进入 S1");
      }
      click("确认先验，进入 S2"); tick(900);
      const confirm = screen.queryByRole<HTMLButtonElement>("button", { name: "确认本批 8 项验证值" });
      if (confirm && !confirm.disabled) fireEvent.click(confirm);
      click("进入 S3"); tick();
    }
    if (action !== "S0 参数不变") {
      click("确认本批 8 项验证值"); click("进入 R2"); tick(); click("确认本批 4 项验证值"); click("进入收敛"); tick();
    }
    click("进入 S4 候选输出"); tick(900); click("进入 S5 多目标配比搜索"); tick(900);
    if (action === "S0 参数不变") {
      expect(viewedStep()).toBe("2"); click("查看 T1 高温接受"); expect(document.querySelector(".ht-s5-readonly-value")?.textContent).toContain("502");
    } else {
      expect(viewedStep()).toBe("0"); click("开始邻域搜索"); tick(700); expect(field().value).toBe("502"); expect(button("进入 T2 继续爬升").disabled).toBe(true);
    }
  }, 30000); // Revisit S0/S1 and replay through S5 under full-suite CI load.
});
