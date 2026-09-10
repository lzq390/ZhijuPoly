/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HighThroughputWorkflowDemoPage } from "../HighThroughputWorkflowDemoPage";
import { highThroughputDemoScenario as scenario, type HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
const button = (name: string | RegExp) => screen.getByRole<HTMLButtonElement>("button", { name });
const click = (name: string | RegExp) => fireEvent.click(button(name));
const tick = (ms = 1400) => act(() => vi.advanceTimersByTime(ms));
const input = (id = "PI-2326", label = "Tg") => screen.getByRole<HTMLInputElement>("spinbutton", { name: `${id} ${label} 演示验证值` });
const change = (id: string, value: string, label = "Tg") => fireEvent.change(input(id, label), { target: { value } });
const target = (label: string) => fireEvent.click(screen.getByRole("tab", { name: label }));
const state = () => document.querySelector<HTMLElement>(".ht-s3-stage")!.dataset;
function loadPrior(key: HighThroughputTargetKey) {
  const label = scenario.targets.find((item) => item.key === key)!.shortLabel;
  const toggle = screen.queryByRole("button", { name: `展开 ${label} Agent` });
  if (toggle) fireEvent.click(toggle);
  click(`上传样例（${label}）`);
}
function enterS3(configure?: () => void) {
  vi.useFakeTimers();
  const view = render(<HighThroughputWorkflowDemoPage onBackHome={() => undefined} />);
  configure?.();
  click("确认场景设置，进入 S1");
  scenario.targets.forEach((item) => loadPrior(item.key));
  tick(1000);
  target("Tg");
  click("确认先验，进入 S2"); tick(900);
  click("确认本批 8 项验证值"); click("进入 S3"); tick();
  view.container.querySelector<HTMLElement>(".ht-scroll-region")!.scrollTo = vi.fn();
  return view;
}
function toR2() { click("确认本批 8 项验证值"); click("进入 R2"); tick(); }
function toConvergence() { toR2(); click("确认本批 4 项验证值"); click("进入收敛"); tick(); }
function fromS2() {
  const confirm = screen.queryByRole("button", { name: "确认本批 8 项验证值" });
  if (confirm) fireEvent.click(confirm);
  click("进入 S3"); tick();
}

// Every case enters from S0 and renders the full candidate SVG across stages.
// CI needs a workflow budget; business timers remain fake and assertions unchanged.
describe("S3 单性质迭代工作台", { timeout: 30000 }, () => {
  it("继承外框与披露，R1 不重复 S2 输入，四个 Agent 默认独立收起", () => {
    const view = enterS3();
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 2, name: "单性质迭代与验证回流" }));
    expect(view.container.querySelector(".ht-s3-board.np-sw-accented-surface")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-scroll-region")).toHaveLength(1);
    expect(view.container.querySelectorAll(".ht-agent-orbit, .ht-next-step-control, .ht-prior-workflow-grid")).toHaveLength(0);
    expect(screen.getByText(/不重训模型，不改变预设热点/)).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-s1-agent-flyout[hidden]")).toHaveLength(4);
    expect(screen.getAllByRole("button", { name: /^展开 .* Agent$/ })).toHaveLength(4);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expect(input().value).not.toBe("");
    expect(screen.queryByRole("spinbutton", { name: /PI-1973/ })).toBeNull();
    expect(view.container.querySelectorAll(".ht-doe-sample")).toHaveLength(12);
    expect(view.container.querySelectorAll(".ht-tested-sample")).toHaveLength(2);
    expect(view.container.querySelectorAll(".ht-recommended-sample-node")).toHaveLength(2);
    expect(button("R2 未开始").disabled).toBe(true);
    expect(button("收敛 未开始").disabled).toBe(true);
    expect(button("进入 R2").disabled).toBe(true);
    expect(view.container.querySelectorAll('input[type="file"]')).toHaveLength(0);
  });

  it("8→4→0 两步门禁，空值就地报错、有效修改撤销确认，R2 为全宽单卡", () => {
    const view = enterS3();
    click("确认本批 8 项验证值");
    change("PI-2326", "300");
    expect(button("进入 R2").disabled).toBe(true);
    change("PI-2326", "");
    expect(input().getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("请输入有效数值");
    expect(screen.getByText("待补 1 项：Tg 1 项")).not.toBeNull();
    expect(button("确认本批 8 项验证值").disabled).toBe(true);
    target("CTE"); change("PI-085", "", "CTE");
    expect(screen.getByText("待补 2 项：Tg 1 项、CTE 1 项")).not.toBeNull();
    change("PI-085", "20", "CTE"); target("Tg"); change("PI-2326", "300");
    toR2();
    expect(state().progressRound).toBe("1");
    expect(screen.getAllByRole("spinbutton")).toHaveLength(1);
    expect(input("PI-1013").value).not.toBe("");
    expect(view.container.querySelector(".ht-validation-single")).not.toBeNull();
    expect(view.container.querySelectorAll(".ht-tested-sample")).toHaveLength(4);
    expect(button("进入收敛").disabled).toBe(true);
    click("确认本批 4 项验证值"); click("进入收敛"); tick();
    expect(state().progressRound).toBe("2");
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.queryByRole("button", { name: /确认本批/ })).toBeNull();
    expect(view.container.querySelectorAll(".ht-s3-comparison-card")).toHaveLength(2);
    expect(view.container.querySelectorAll(".ht-tested-sample")).toHaveLength(5);
    expect(button("进入 S4 候选输出").disabled).toBe(false);
    expect(button("重演 S3")).not.toBeNull();
  });

  it("收敛使用独立对照图标与标题，不将中文名称放进轮次编号框", () => {
    enterS3();
    const convergence = button("收敛 未开始");
    const marker = convergence.querySelector(".ht-s3-round-convergence")!;
    expect(marker.getAttribute("aria-hidden")).toBe("true");
    expect(marker.querySelector("svg")).not.toBeNull();
    expect(marker.textContent).toBe("");
    expect(convergence.querySelector(".ht-s3-round-number")).toBeNull();
    expect(within(convergence).getByText("收敛对照")).not.toBeNull();
    expect(convergence.disabled).toBe(true);

    toConvergence();
    expect(button("收敛 当前")).toBe(convergence);
    expect(convergence.getAttribute("aria-current")).toBe("step");
    click("R1 已完成");
    expect(state().viewRound).toBe("0");
    fireEvent.keyDown(button("R1 已完成"), { key: "End" });
    expect(state().viewRound).toBe("2");
    expect(document.activeElement).toBe(convergence);
  });

  it("证据摘要仅对数字使用等宽标识，四个预设输出 ID 使用独立技术文本样式", () => {
    const view = enterS3();
    const counters = () => Array.from(view.container.querySelectorAll(".ht-s3-summary-status b"), (element) => element.textContent);
    expect(counters()).toEqual(["12", "2"]);
    expect(view.container.querySelector(".ht-s3-summary-status")?.textContent).toContain("每目标证据 12 DOE + 2 回流");
    toConvergence();
    expect(counters()).toEqual(["12", "5"]);
    for (const item of scenario.targets) {
      target(item.shortLabel);
      const component = scenario.formulation.components.find((component) => component.sourceTargetKey === item.key)!;
      const outputId = view.container.querySelector(".ht-s3-output-id");
      expect(outputId?.textContent).toBe(component.id);
      expect(outputId?.closest("button")?.textContent).toContain("预设输出候选");
    }
  });

  it("只读回看按当轮快照，不回滚进度，保留详情且底栏仅返回当前轮次", () => {
    const view = enterS3();
    const initialStar = view.container.querySelector(".ht-current-best-marker")?.getAttribute("data-candidate-id");
    change("PI-2326", "9999"); toR2();
    const detail = view.container.querySelector<HTMLElement>(".ht-s2-structure-fields")!;
    click("展开结构详情"); click("R1 已完成");
    expect(state().progressRound).toBe("1"); expect(state().viewRound).toBe("0");
    expect(screen.getByText("回看模式 · R1 已完成")).not.toBeNull();
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(view.container.querySelector(".ht-s3-footer")?.querySelectorAll("button")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "重置本批验证值" })).toBeNull();
    expect(screen.queryByRole("button", { name: "返回 S2 推荐验证" })).toBeNull();
    expect(view.container.querySelector(".ht-current-best-marker")?.getAttribute("data-candidate-id")).toBe(initialStar);
    expect(view.container.querySelector("[data-surface-snapshot]")?.getAttribute("data-surface-snapshot")).toBe("round1");
    expect(view.container.querySelectorAll(".ht-tested-sample")).toHaveLength(2);
    expect(view.container.querySelector(".ht-validation-readonly-value")?.textContent).toContain("9999");
    fireEvent.keyDown(button("查看 PI-2842 推荐点及结构详情"), { key: " " });
    expect(button("查看 PI-2842 推荐详情").getAttribute("aria-pressed")).toBe("true");
    target("CTE");
    expect(view.container.querySelector(".ht-s2-structure-fields")).toBe(detail); expect(detail.hidden).toBe(false);
    click("返回当前轮次"); expect(state().viewRound).toBe("1");
    expect(screen.getAllByRole("spinbutton")).toHaveLength(1);
    expect(button("进入收敛").disabled).toBe(true);
    fireEvent.keyDown(button("R2 当前"), { key: "Home" }); expect(state().viewRound).toBe("0");
    fireEvent.keyDown(button("R1 已完成"), { key: "End" }); expect(state().viewRound).toBe("1");
  });

  it("确认本身不更新最优，推进后图、摘要、Agent 一致，收敛仍并列预设 p1", () => {
    const view = enterS3();
    const initial = view.container.querySelector(".ht-s3-record-summary")!.textContent;
    change("PI-2326", "9999"); click("确认本批 8 项验证值");
    expect(view.container.querySelector(".ht-s3-record-summary")!.textContent).toBe(initial);
    click("进入 R2"); tick();
    click("展开 Tg Agent");
    expect(view.container.querySelector(".ht-current-best-marker")?.getAttribute("data-candidate-id")).toBe("PI-2326");
    expect(view.container.querySelector(".ht-s3-record-summary")?.textContent).toContain("9,999");
    expect(view.container.querySelector(".ht-s3-agent-best")?.textContent).toContain("9,999");
    expect(view.container.querySelector(".ht-s3-script-summary")?.textContent).toContain("PI-2842");
    expect(view.container.querySelector("[data-surface-snapshot]")?.getAttribute("data-surface-snapshot")).toBe("round2");
    click("确认本批 4 项验证值"); click("进入收敛"); tick();
    const cards = view.container.querySelectorAll(".ht-s3-comparison-card");
    expect(cards[0].textContent).toContain("PI-2326"); expect(cards[0].textContent).toContain("9,999");
    expect(cards[1].textContent).toContain("p1"); expect(cards[1].textContent).toContain("PI-1013");
    fireEvent.click(cards[0]); expect(screen.getByRole("region", { name: "PI-2326 候选详情" })).not.toBeNull();
    fireEvent.click(cards[1]); expect(screen.getByRole("region", { name: "PI-1013 候选详情" })).not.toBeNull();
  });

  it("结构详情跨候选、目标、轮次和收敛保留展开，离开 S3 再进入默认收起", () => {
    const view = enterS3(); click("展开结构详情");
    const fields = view.container.querySelector<HTMLElement>(".ht-s2-structure-fields")!;
    click("查看 PI-2842 推荐详情"); expect(fields.hidden).toBe(false);
    act(() => input().focus()); expect(document.activeElement).toBe(input());
    expect(button("查看 PI-2326 推荐详情").getAttribute("aria-pressed")).toBe("true");
    target("CTE"); expect(view.container.querySelector(".ht-s2-structure-fields")).toBe(fields);
    toConvergence();
    expect(view.container.querySelector(".ht-s2-structure-fields")).toBe(fields); expect(fields.hidden).toBe(false);
    click("R1 已完成"); expect(view.container.querySelector(".ht-s2-structure-fields")).toBe(fields);
    click("返回当前轮次"); click("返回 S2 推荐验证"); fromS2();
    expect(state().viewRound).toBe("2");
    expect(view.container.querySelector<HTMLElement>(".ht-s2-structure-fields")?.hidden).toBe(true);
  });

  it("图点键盘选择只滚动内部工作区，Agent 排除内框点击且目标页签支持键盘", () => {
    const view = enterS3();
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    scroll.scrollTo = vi.fn();
    vi.spyOn(scroll, "getBoundingClientRect").mockReturnValue({ top: 0, bottom: 300 } as DOMRect);
    const card = view.container.querySelectorAll(".ht-s2-validation-candidate")[1];
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue({ top: 800, bottom: 1000 } as DOMRect);
    const outerScroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    fireEvent.keyDown(button("查看 PI-2842 推荐点并编辑 Tg 演示验证值"), { key: "Enter" });
    expect(scroll.scrollTo).toHaveBeenCalled(); expect(outerScroll).not.toHaveBeenCalled();
    expect(button("查看 PI-2842 推荐详情").getAttribute("aria-pressed")).toBe("true");
    click("展开 Tg Agent"); target("CTE");
    const agent = view.container.querySelector('[data-agent-theme="tg"]')!;
    fireEvent.click(agent.querySelector(".ht-s1-agent-target")!);
    expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(agent.querySelector(".ht-s3-agent-data")!);
    expect(screen.getByRole("tab", { name: "CTE" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(agent.querySelector(".ht-s1-agent-status")!);
    expect(screen.getByRole("tab", { name: "Tg" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Tg" }), { key: "End" });
    expect(screen.getByRole("tab", { name: "Modulus" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Modulus" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Tg" }).getAttribute("aria-selected")).toBe("true");
  });

  it("推进期间锁定编辑、目标、轮次、重置与返回，但仍可收起 Agent；卸载清理计时器", () => {
    const view = enterS3(); click("展开 Tg Agent"); click("确认本批 8 项验证值");
    const scheduled = vi.spyOn(window, "setTimeout");
    const cleared = vi.spyOn(window, "clearTimeout");
    click("进入 R2");
    const transitionTimer = scheduled.mock.results[scheduled.mock.calls.findIndex((call) => call[1] === 1400)].value;
    expect(input().disabled).toBe(true);
    expect(button("R1 当前").disabled).toBe(true);
    expect(button("重置本批验证值").disabled).toBe(true);
    expect(button("返回 S2 推荐验证").disabled).toBe(true);
    expect(button("本批已确认").disabled).toBe(true);
    expect(button("展开 CTE Agent").disabled).toBe(true);
    expect(screen.getByRole<HTMLButtonElement>("tab", { name: "CTE" }).disabled).toBe(true);
    expect(screen.getByText(/正在演示验证回流，切换到 R2/)).not.toBeNull();
    expect(button("收起 Tg Agent").disabled).toBe(false); click("收起 Tg Agent");
    expect(button("展开 Tg Agent").disabled).toBe(true);
    view.unmount(); expect(cleared).toHaveBeenCalledWith(transitionTimer);
    tick(); expect(vi.getTimerCount()).toBe(0);
  });

  it("返回 S2 不改数据可续接进度与确认，而非恢复历史查看轮次", () => {
    enterS3(); toR2(); change("PI-1013", "777"); click("确认本批 4 项验证值");
    click("R1 已完成"); click("返回当前轮次"); click("返回 S2 推荐验证");
    expect(button("进入 S3").disabled).toBe(false); fromS2();
    expect(state().progressRound).toBe("1"); expect(state().viewRound).toBe("1");
    expect(input("PI-1013").value).toBe("777"); expect(button("进入收敛").disabled).toBe(false);
  });

  it("仅 S3 为焦点滚动预留实际 sticky 底栏高度，离开后清除", () => {
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("ht-s3-footer") ? 110 : 0;
    });
    const view = enterS3();
    const scroll = view.container.querySelector<HTMLElement>(".ht-scroll-region")!;
    expect(scroll.style.getPropertyValue("--ht-s3-footer-height")).toBe("110px");
    click("返回 S2 推荐验证");
    expect(scroll.style.getPropertyValue("--ht-s3-footer-height")).toBe("");
  });

  it.each(["S2 编辑", "S2 重置", "S1 上传成功", "S1 上传失败", "S1 重置", "S0 参数变化", "S0 重置", "S0 参数不变"])("%s 的上游衔接正确保留值并管理下游确认", (action) => {
    enterS3(); change("PI-2326", "777"); toR2(); change("PI-1013", "888"); click("确认本批 4 项验证值");
    click("返回 S2 推荐验证");
    if (action === "S2 编辑") change("PI-1973", "555");
    else if (action === "S2 重置") click("重置 S2 验证值");
    else {
      click("返回 S1 先验导入");
      if (action === "S1 上传成功") { loadPrior("tg"); tick(1000); }
      if (action === "S1 上传失败") {
        click("展开 Tg Agent");
        const fileInput = document.querySelector<HTMLInputElement>('input[type="file"]')!;
        fireEvent.change(fileInput, { target: { files: [new File(["invalid"], "invalid.csv")] } });
        expect(screen.getByRole("alert").textContent).toContain("上传失败：文件不匹配");
        expect(button("确认先验，进入 S2").disabled).toBe(true);
        loadPrior("tg"); tick(1000);
      }
      if (action === "S1 重置") { click("重置 S1 上传数据"); scenario.targets.forEach((item) => loadPrior(item.key)); tick(1000); }
      if (action.startsWith("S0")) {
        click("返回 S0 场景设置");
        if (action === "S0 参数变化") fireEvent.change(screen.getByLabelText("Tg 目标值"), { target: { value: "400" } });
        if (action === "S0 重置") click("恢复默认场景参数");
        click("确认场景设置，进入 S1");
      }
      click("确认先验，进入 S2"); tick(900);
    }
    fromS2();
    target("Tg");
    if (action === "S0 参数不变") {
      expect(state().progressRound).toBe("1"); expect(button("进入收敛").disabled).toBe(false);
    } else {
      expect(state().progressRound).toBe("0"); expect(state().viewRound).toBe("0");
      expect(button("进入 R2").disabled).toBe(true); expect(input().value).toBe("777");
      toR2(); expect(input("PI-1013").value).toBe("888"); expect(button("进入收敛").disabled).toBe(true);
    }
  });

  it("小数阈值场景返回 S0 后不修改再确认，保留 S2 确认和 S3 轮次进度", () => {
    enterS3(() => {
      fireEvent.change(screen.getByRole("spinbutton", { name: "Tg 目标值" }), { target: { value: "300.25" } });
      fireEvent.change(screen.getByRole("spinbutton", { name: "Modulus 目标值" }), { target: { value: "3.01" } });
    });
    change("PI-2326", "777.25");
    toR2();
    change("PI-1013", "888.25");
    click("确认本批 4 项验证值");
    click("返回 S2 推荐验证");
    click("返回 S1 先验导入");
    click("返回 S0 场景设置");
    expect(screen.getByRole<HTMLInputElement>("spinbutton", { name: "Tg 目标值" }).value).toBe("300.25");
    expect(screen.getByRole<HTMLInputElement>("spinbutton", { name: "Modulus 目标值" }).value).toBe("3.01");
    click("确认场景设置，进入 S1");
    expect(button("确认先验，进入 S2").disabled).toBe(false);
    click("确认先验，进入 S2"); tick(900);
    expect(button("进入 S3").disabled).toBe(false);
    click("进入 S3"); tick();
    expect(state().progressRound).toBe("1");
    expect(state().viewRound).toBe("1");
    expect(button("进入收敛").disabled).toBe(false);
    expect(input("PI-1013").value).toBe("888.25");
    click("R1 已完成");
    expect(document.querySelector(".ht-validation-readonly-value")?.textContent).toContain("777.25");
  });

  it("当前批重置不改已回流批次；重演原生弹窗取消与确认分别保留和恢复 12 项", () => {
    const view = enterS3(); const r1Default = input().value;
    change("PI-2326", "777"); toR2(); const r2Default = input("PI-1013").value;
    change("PI-1013", "888"); click("确认本批 4 项验证值"); click("重置本批验证值");
    expect(input("PI-1013").value).toBe(r2Default); expect(button("进入收敛").disabled).toBe(true);
    click("R1 已完成"); expect(view.container.querySelector(".ht-validation-readonly-value")?.textContent).toContain("777");
    click("返回当前轮次"); click("确认本批 4 项验证值"); click("进入收敛"); tick();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    click("重演 S3"); expect(state().progressRound).toBe("2");
    confirm.mockReturnValue(true); click("重演 S3");
    expect(confirm).toHaveBeenCalledTimes(2); expect(state().progressRound).toBe("0"); expect(input().value).toBe(r1Default);
    expect(button("进入 R2").disabled).toBe(true);
    toR2(); expect(input("PI-1013").value).toBe(r2Default); expect(button("进入收敛").disabled).toBe(true);
    click("返回 S2 推荐验证"); expect(button("进入 S3").disabled).toBe(false);
  });

  it("S0 配置阈值在图下、Agent、验证区一致，摄氏度统一", () => {
    const view = enterS3(() => {
      fireEvent.change(screen.getByLabelText("Tg 目标值"), { target: { value: "500" } });
      fireEvent.change(screen.getByLabelText("Modulus 目标值"), { target: { value: "4.01" } });
    });
    click("展开 Tg Agent");
    const agent = within(view.container.querySelector('[data-agent-theme="tg"]') as HTMLElement);
    expect(agent.getByText(/500/)).not.toBeNull();
    expect(view.container.querySelector(".ht-s3-record-summary")?.textContent).toContain("500 °C");
    expect(view.container.querySelector(".ht-s2-validation-threshold")?.textContent).toContain("500 °C");
    expect(view.container.querySelector(".ht-s3-stage")?.textContent).not.toContain("degC");
    click("展开 Modulus Agent");
    expect(view.container.querySelector('[data-agent-theme="modulus"] .ht-s1-agent-target')?.textContent).toContain("4.01");
    expect(view.container.querySelector(".ht-s2-validation-threshold")?.textContent).toContain("4.01 GPa");
  });
});
