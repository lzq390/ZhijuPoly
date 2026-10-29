// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PolymerMdJob, PolymerMdJobPage } from "../../types/polymerMd";
import { POLYMER_MD_DEFAULTS } from "./config";
import { PolymerMdTaskCenter } from "./PolymerMdTaskCenter";

const started = "2026-10-28T09:38:19Z";
function job(id: string, change: Partial<PolymerMdJob> = {}): PolymerMdJob {
  return { job_id: id, status: "running", quality_status: "not_evaluated", scientific_request: POLYMER_MD_DEFAULTS,
    stage: "eq3", stage_runs: [], artifacts: [], results: {}, created_at: started, updated_at: started, started_at: started, ...change };
}
const history = (items: PolymerMdJob[], page = 1, total = items.length): PolymerMdJobPage => ({ items, page, total, page_size: 10 });
const defaults = () => ({ history: history([]), page: 1, error: null, busy: null, guest: false, selectedId: null,
  onSelect: vi.fn(), onCancel: vi.fn(), onPageChange: vi.fn(), onRefresh: vi.fn(), onLogin: vi.fn() });

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("polymer MD task center", () => {
  it("keeps execution and scientific quality distinct and binds row actions to job IDs after reordering", () => {
    const running = job("running-id");
    const completed = job("completed-id", { status: "completed", quality_status: "not_converged", finished_at: "2026-10-28T10:38:19Z" });
    const props = { ...defaults(), history: history([running, completed], 1, 23), selectedId: completed.job_id };
    const view = render(<PolymerMdTaskCenter {...props} />);
    expect(screen.getByText("共 23 个任务")).toBeTruthy();
    const finishedRow = screen.getByRole("listitem", { name: "任务 completed-id" });
    expect(finishedRow.className).toContain("is-selected");
    expect(within(finishedRow).getByText("执行完成")).toBeTruthy();
    expect(within(finishedRow).getByText("尚未收敛")).toBeTruthy();
    expect(within(finishedRow).queryByRole("button", { name: "取消" })).toBeNull();
    expect(within(finishedRow).getByText("运行时长").nextElementSibling?.textContent).toBe("1 小时 0 分 0 秒");
    fireEvent.click(within(finishedRow).getByRole("button", { name: "查看任务" }));
    expect(props.onSelect).toHaveBeenLastCalledWith(completed.job_id);
    view.rerender(<PolymerMdTaskCenter {...props} history={history([completed, running], 1, 23)} />);
    fireEvent.click(within(screen.getByRole("listitem", { name: "任务 running-id" })).getByRole("button", { name: "取消" }));
    expect(props.onCancel).toHaveBeenLastCalledWith(running.job_id);
    expect(screen.queryByText(/队列位置|运行中任务数|完成任务数/)).toBeNull();
  });

  it("shows every blend component and its own requested DP or molecular weight without substituting n_atom", () => {
    const longSmiles = `*${"CC".repeat(300)}*`;
    const blend = job("blend-id", { scientific_request: { ...POLYMER_MD_DEFAULTS, n_atom: 4999, n_chain: 5, model_configuration: {
      version: 1, mode: "blend", components: [
        { id: "A", smiles: longSmiles, n_chain: 2, chain_length: { mode: "dp", value: 60 } },
        { id: "B", smiles: "*CCO*", n_chain: 3, chain_length: { mode: "molecular_weight", value: 14000 } }
      ]
    } } });
    render(<PolymerMdTaskCenter {...defaults()} history={history([blend])} />);
    expect(screen.getByRole("heading", { name: "聚合物共混物" })).toBeTruthy();
    expect(screen.getByLabelText("组分 A SMILES").textContent).toBe(longSmiles);
    expect(screen.getByLabelText("组分 A SMILES").getAttribute("title")).toBe(longSmiles);
    expect(screen.getByLabelText("组分 B SMILES").textContent).toBe("*CCO*");
    expect(screen.getByLabelText("组分 A SMILES").parentElement?.querySelector("small")?.textContent).toBe("请求 2 条链 · 每链目标聚合度 DP 60");
    expect(screen.getByLabelText("组分 B SMILES").parentElement?.querySelector("small")?.textContent).toBe("请求 3 条链 · 每链目标分子量 14,000 g/mol");
    expect(screen.queryByText(/4,999/)).toBeNull();
  });

  it("retains random repeat-unit ratios and block sequences as requested composition", () => {
    const components = [{ id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }];
    render(<PolymerMdTaskCenter {...defaults()} history={history([
      job("random", { scientific_request: { ...POLYMER_MD_DEFAULTS, model_configuration: { version: 1, mode: "random", components, ratios: [0.25, 0.75], chain_length: { mode: "dp", value: 20 } } } }),
      job("block", { scientific_request: { ...POLYMER_MD_DEFAULTS, model_configuration: { version: 1, mode: "block", components, blocks: [{ component_id: "A", dp: 8 }, { component_id: "B", dp: 12 }] } } })
    ])} />);
    expect([...screen.getByRole("listitem", { name: "任务 random" }).querySelectorAll(".np-pmd-task-components small")].map(element => element.textContent)).toEqual(["请求重复单元数比例 25%", "请求重复单元数比例 75%"]);
    expect(screen.getByRole("listitem", { name: "任务 block" }).querySelector(".np-pmd-task-scope")?.textContent).toBe("目标嵌段：A×8 → B×12");
  });

  it("hides stale page rows during pagination, prevents rapid paging and allows recovery after a failed page", () => {
    const props = { ...defaults(), history: history([job("page-one")], 1, 23) };
    const view = render(<PolymerMdTaskCenter {...props} />);
    expect((screen.getByRole("button", { name: "上一页" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(props.onPageChange).toHaveBeenCalledWith(2);
    view.rerender(<PolymerMdTaskCenter {...props} page={2} />);
    expect(screen.getByRole("status").textContent).toContain("正在读取第 2 页任务");
    expect(screen.queryByRole("listitem")).toBeNull();
    for (const name of ["上一页", "下一页"]) expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<PolymerMdTaskCenter {...props} page={2} error="读取请求失败" />);
    expect(screen.getByRole("alert").textContent).toContain("读取请求失败");
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(props.onPageChange).toHaveBeenLastCalledWith(1);
    fireEvent.click(screen.getByRole("button", { name: "重试读取任务" }));
    expect(props.onRefresh).toHaveBeenCalledOnce();
    view.rerender(<PolymerMdTaskCenter {...props} page={3} history={history([job("page-three")], 3, 23)} />);
    expect(screen.getByText("第 3 页 / 共 3 页")).toBeTruthy();
    expect((screen.getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("distinguishes guest, first loading, empty and failed refresh states without hiding readable records", () => {
    const props = defaults();
    const view = render(<PolymerMdTaskCenter {...props} history={null} guest />);
    expect(screen.getByText("登录后查看计算任务")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "登录账号" }));
    expect(props.onLogin).toHaveBeenCalledOnce();
    view.rerender(<PolymerMdTaskCenter {...props} history={null} />);
    expect(screen.getByRole("status").textContent).toContain("正在读取第 1 页任务");
    view.rerender(<PolymerMdTaskCenter {...props} />);
    expect(screen.getByText("暂无任务记录。")).toBeTruthy();
    view.rerender(<PolymerMdTaskCenter {...props} history={history([job("retained")])} error="刷新超时" />);
    expect(screen.getByRole("alert").textContent).toContain("当前保留上次读取的任务");
    expect(screen.getByRole("listitem", { name: "任务 retained" })).toBeTruthy();
  });

  it("shares one clock across running and cancelling rows and leaves unstarted tasks uncounted", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(started) + 1000);
    const props = { ...defaults(), history: history([job("run"), job("cancel", { status: "cancel_requested" }), job("queue", { status: "queued", started_at: null })]) };
    const view = render(<PolymerMdTaskCenter {...props} />);
    expect(vi.getTimerCount()).toBe(1);
    expect(screen.getAllByText("运行时长").map(label => label.nextElementSibling?.textContent)).toEqual(["1 秒", "1 秒", "尚未开始"]);
    expect(screen.getAllByText("尚未开始")).toHaveLength(2);
    expect((screen.getByRole("button", { name: "正在取消" }) as HTMLButtonElement).disabled).toBe(true);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getAllByText("运行时长").map(label => label.nextElementSibling?.textContent)).toEqual(["2 秒", "2 秒", "尚未开始"]);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
