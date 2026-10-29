// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSession, installSession, retireSession } from "../auth/session";
import { POLYMER_MD_PENDING_KEY, POLYMER_MD_PENDING_MAX_AGE_MS, createPolymerMdPending } from "../services/polymerMdPending";
import { PolymerMdApiError } from "../services/polymerMdApi";
import type { StructureWorkspaceContext } from "../types";
import type { PolymerMdCapabilities, PolymerMdJob } from "../types/polymerMd";
import { PolymerMdSimulationPage } from "./PolymerMdSimulationPage";
import { MODEL_LABELS } from "./polymer-md/PolymerMdModelFields";
import { POLYMER_MD_DEFAULTS, POLYMER_MD_DRAFT_KEY, savePolymerMdDraft } from "./polymer-md/config";
import { defaultPolymerMdUserConfiguration, mapPolymerMdUserConfiguration, polymerMdUserConfigurationSpec, POLYMER_MD_USER_CONFIGURATION_KEY, readPolymerMdUserConfiguration, savePolymerMdUserConfiguration } from "./polymer-md/userConfiguration";
import { POLYMER_MD_BASE_PROPERTIES, POLYMER_MD_SINGLE_POINT_PROPERTIES } from "./polymer-md/propertyGroups";
const mocks = vi.hoisted(() => ({ capabilities: vi.fn(), status: vi.fn(), jobs: vi.fn(), job: vi.fn(), create: vi.fn(), cancel: vi.fn(), clear: vi.fn(), remove: vi.fn(), trajectory: vi.fn(), distance: vi.fn(), download: vi.fn(), logs: vi.fn() }));
vi.mock("../services/polymerMdApi", async importOriginal => ({ ...await importOriginal<typeof import("../services/polymerMdApi")>(), fetchPolymerMdCapabilities: mocks.capabilities, fetchPolymerMdStatus: mocks.status, fetchPolymerMdJobs: mocks.jobs, fetchPolymerMdJob: mocks.job, createPolymerMdJob: mocks.create, cancelPolymerMdJob: mocks.cancel, clearPolymerMdArtifacts: mocks.clear, deletePolymerMdJob: mocks.remove, fetchPolymerMdTrajectory: mocks.trajectory, calculatePolymerMdDistance: mocks.distance, downloadPolymerMdArtifact: mocks.download, fetchPolymerMdStageLogs: mocks.logs }));
vi.mock("./MdSimulationDemoPage", () => ({ MdSimulationDemoPage: () => <p>固定示例演示内容</p> }));
vi.mock("./md-simulation/MdStructureInput", () => ({ MdStructureInput: ({ value, onChange, error }: { value: string; onChange: (s: string) => void; error: string | null }) => <label>重复单元 SMILES<textarea value={value} onChange={e => onChange(e.target.value)} />{error}</label> }));
vi.mock("./md-simulation/MdTrajectoryExplorer", () => ({ MdTrajectoryExplorer: ({ points, stageLabel, frameLabel, distanceUsePbc, onSelectionChange, onCalculate, distance }: { points: Array<{ atom_id: number; chain_id: number; atom_type: string }>; stageLabel: string; frameLabel: string; distanceUsePbc: boolean; onSelectionChange: (s: unknown) => void; onCalculate: () => void; distance: unknown }) => <div>真实坐标原子 {points[0]?.atom_id}<p>轨迹来源：{stageLabel}，{frameLabel}</p><p>距离周期边界：{distanceUsePbc ? "启用" : "关闭"}</p><button onClick={() => onSelectionChange([points[0], points[1]])}>选择原子对</button><button onClick={onCalculate}>计算当前阶段距离</button>{distance ? <p>当前阶段距离已返回</p> : null}</div> }));
const caps: PolymerMdCapabilities = { enabled: true, submit_enabled: true, schema_ready: true, worker_ready: true, protocol_version: 1, supported_forcefields: ["GAFF", "GAFF2", "GAFF2_mod", "Dreiding", "Dreiding_UT"], supported_terminals: ["CH3", "H", "OH"], supported_tacticities: ["atactic", "isotactic", "syndiotactic"], supported_calculations: ["basic", "basic_tc"], trajectory_available: true, distance_available: true, limits: { per_owner_active: 1, global_active: 3, execution: 1, max_total_atoms: 20000, max_rnemd_atoms: 60000 } };
const stage = { stage_run_id: "eq3-attempt-1", stage: "eq3", attempt: 1, status: "completed", quality_status: "passed" as const, results: {}, quality: {}, errors: [] };
function job(change: Partial<PolymerMdJob> = {}): PolymerMdJob { return { job_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", status: "running", quality_status: "not_evaluated", scientific_request: { ...POLYMER_MD_DEFAULTS, seed: 42 }, stage: "eq3", stage_runs: [stage], artifacts: [], results: {}, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z", ...change }; }
const structure = { smiles: "", getCurrentSmiles: async () => "*CC*" } as StructureWorkspaceContext;
function page() { return render(<PolymerMdSimulationPage structure={structure} onEditStructure={vi.fn()} />); }
function choose(name: string, label: string) {
  fireEvent.click(screen.getByRole("combobox", { name }));
  const menu = within(screen.getByRole("listbox", { name }));
  fireEvent.click(menu.getByText(label, { selector: "strong", exact: true }).closest('[role="option"]')!);
}

beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); window.history.replaceState(null, "", "/md-simulation"); mocks.capabilities.mockResolvedValue(caps); mocks.status.mockResolvedValue(caps); mocks.jobs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 }); mocks.job.mockResolvedValue(job()); mocks.create.mockResolvedValue(job()); mocks.cancel.mockResolvedValue(job({ status: "cancel_requested" })); mocks.clear.mockResolvedValue(job({ artifacts: [] })); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe("formal polymer MD page", () => {
  it("shows actual execution times in task history and the selected result without mutating a job", async () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-29T01:41:23Z"));
    const started = "2026-10-28T09:38:19.714180+00:00";
    const running = job({ started_at: started });
    const finished = job({ job_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeef", status: "completed", started_at: started, finished_at: "2026-10-28T10:39:22.714180+00:00" });
    const queued = job({ job_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeff", status: "queued", started_at: null });
    mocks.jobs.mockResolvedValue({ items: [running, finished, queued], total: 3, page: 1, page_size: 10 });
    mocks.job.mockResolvedValue(running);
    page();
    fireEvent.click(screen.getByRole("tab", { name: "任务中心" }));
    await screen.findAllByRole("button", { name: "查看任务" });
    const timings = screen.getAllByLabelText("任务执行时间");
    expect(within(timings[0]).getByText("2026-10-28 17:38:19")).toBeTruthy();
    expect(within(timings[0]).getByText("运行时长").nextElementSibling?.textContent).toBe("16 小时 3 分 3 秒");
    expect(within(timings[0]).getByText("北京时间")).toBeTruthy();
    expect(within(timings[1]).getByText("运行时长").nextElementSibling?.textContent).toBe("1 小时 1 分 3 秒");
    expect(within(timings[2]).getAllByText("尚未开始")).toHaveLength(2);
    fireEvent.click(screen.getAllByRole("button", { name: "查看任务" })[0]);
    await screen.findByRole("heading", { name: "任务详情" });
    const overview = screen.getByLabelText("任务执行时间");
    expect(within(overview).getByText("2026-10-28 17:38:19")).toBeTruthy();
    expect(within(overview).getByText("运行时长").nextElementSibling?.textContent).toBe("16 小时 3 分 3 秒");
    for (const mutation of [mocks.create, mocks.cancel, mocks.clear, mocks.remove]) expect(mutation).not.toHaveBeenCalled();
  });

  it("uses one workbench and preserves formal inputs while switching modes and workspaces", async () => {
    mocks.jobs.mockResolvedValue({ items: [job()], total: 1, page: 1, page_size: 10 });
    const view = page();
    const surface = view.container.querySelector(".np-pmd-workbench-surface")!;
    const toolbar = view.container.querySelector(".np-pmd-module-toolbar")!;
    expect(surface.contains(toolbar)).toBe(false);
    expect(surface.querySelector(".np-pmd-mode-switch")).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("目标温度"), { target: { value: "350" } });
    choose("聚合物力场", "GAFF2_mod");
    fireEvent.click(screen.getByRole("checkbox", { name: "玻璃化转变温度 Tg" }));
    fireEvent.click(screen.getByRole("button", { name: "示例演示" }));
    expect(screen.getByText("固定示例演示内容")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "任务中心" }));
    expect(screen.getByRole("heading", { name: "聚合物 MD 任务中心" })).toBeTruthy();
    expect(view.container.querySelector(".np-pmd-demo-slot")?.hasAttribute("hidden")).toBe(true);
    fireEvent.click(await screen.findByRole("button", { name: "查看任务" }));
    await screen.findByRole("heading", { name: "任务详情" });
    expect(screen.getByRole("heading", { name: "聚合物 MD 结果分析" })).toBeTruthy();
    expect(window.location.search).toContain(job().job_id);
    fireEvent.click(screen.getByRole("tab", { name: "模拟配置" }));
    expect((screen.getByLabelText("目标温度") as HTMLInputElement).value).toBe("350");
    expect(screen.getByRole("combobox", { name: "聚合物力场" }).textContent).toContain("GAFF2_mod");
    expect((screen.getByRole("checkbox", { name: "玻璃化转变温度 Tg" }) as HTMLInputElement).checked).toBe(true);
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("defaults to formal inputs, saves a private draft, and only mounts the fixture on explicit demo selection", async () => {
    page(); await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText("固定示例演示内容")).toBeNull(); expect(screen.getByLabelText("每链目标原子数")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("目标温度"), { target: { value: "320" } }); expect(JSON.parse(sessionStorage.getItem(POLYMER_MD_DRAFT_KEY)!).request.temp).toBe(320);
    fireEvent.click(screen.getByRole("button", { name: "示例演示" })); expect(screen.getByText("固定示例演示内容")).toBeTruthy(); expect(mocks.create).not.toHaveBeenCalled();
  });
  it("retains editable inputs and explains a worker that is not ready", async () => {
    mocks.capabilities.mockResolvedValue({ ...caps, worker_ready: false }); page();
    expect(await screen.findByText("计算服务尚未就绪，请稍后刷新。")).toBeTruthy(); expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true); expect((screen.getByLabelText("目标温度") as HTMLInputElement).disabled).toBe(false); expect(mocks.create).not.toHaveBeenCalled();
  });
  it("marks a single lost status observation as unconfirmed, keeps repeated failures explicit and recovers without changing inputs", async () => {
    page();
    const submit = screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement;
    const toolbar = within(screen.getByLabelText("聚合物 MD 模拟工具栏"));
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.change(screen.getByLabelText("目标温度"), { target: { value: "350" } });
    mocks.capabilities.mockRejectedValueOnce(new Error("Temporary observation failure"));
    fireEvent.click(toolbar.getByRole("button", { name: "刷新" }));
    const unconfirmed = await toolbar.findByText("状态待确认");
    expect(unconfirmed.closest('[role="status"]')?.className).toBe("is-unavailable");
    expect(submit.disabled).toBe(true);
    expect((screen.getByLabelText("目标温度") as HTMLInputElement).value).toBe("350");
    mocks.capabilities.mockRejectedValueOnce(new Error("Repeated observation failure"));
    fireEvent.click(toolbar.getByRole("button", { name: "刷新" }));
    const failed = await toolbar.findByText("连接异常");
    expect(failed.closest('[role="status"]')?.className).toBe("is-error");
    expect(submit.disabled).toBe(true);
    fireEvent.click(toolbar.getByRole("button", { name: "刷新" }));
    await toolbar.findByText("准备就绪");
    expect(submit.disabled).toBe(false);
    expect((screen.getByLabelText("目标温度") as HTMLInputElement).value).toBe("350");
    expect(mocks.create).not.toHaveBeenCalled();
    expect(mocks.status).not.toHaveBeenCalled();
  });
  it("marks a forcefield rejected by the service and lets the user repair the saved choice", async () => {
    mocks.capabilities.mockResolvedValue({ ...caps, supported_forcefields: ["Dreiding"] });
    page();
    const error = await screen.findByRole("alert");
    const forcefield = screen.getByRole("combobox", { name: "聚合物力场" });
    expect(error.textContent).toBe("当前协议不支持此力场。");
    expect(forcefield.textContent).toContain("GAFF2_mod");
    expect(forcefield.getAttribute("aria-invalid")).toBe("true");
    expect(forcefield.getAttribute("aria-describedby")?.split(" ")).toContain(error.id);
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);

    choose("聚合物力场", "Dreiding");
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(forcefield.getAttribute("aria-invalid")).toBe("false");
    expect(mocks.create).not.toHaveBeenCalled();
  });
  it("explains the managed RNEMD replication budget and clears it after reducing the system", async () => {
    mocks.capabilities.mockResolvedValue({ ...caps, limits: { ...caps.limits, max_rnemd_atoms: 20000 } });
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("checkbox", { name: "MP/RNEMD总热导率" }));
    const thermal = within(screen.getByRole("region", { name: "热导方向" }));
    expect(thermal.getByRole("alert").textContent).toBe("复制后的热导体系超过原子数上限。请减少链数或每链规模后重试。");
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByLabelText("传热方向复制倍数")).toBeNull();

    fireEvent.change(screen.getByLabelText("链数"), { target: { value: "6" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(thermal.queryByRole("alert")).toBeNull();
    expect((screen.getByRole("checkbox", { name: "MP/RNEMD总热导率" }) as HTMLInputElement).checked).toBe(true);
    expect(mocks.create).not.toHaveBeenCalled();
  });
  it("explains a blend's atom budget when its aggregate chain field is hidden", async () => {
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("radio", { name: "聚合物共混物" }));
    fireEvent.change(screen.getByLabelText("组分 A 每链目标原子数"), { target: { value: "5000" } });
    fireEvent.change(screen.getByLabelText("组分 B 每链目标原子数"), { target: { value: "5000" } });
    const error = screen.getByRole("alert");
    const limits = screen.getByLabelText("体系规模说明");
    expect(error.textContent).toBe("估算的基础体系超过原子数上限。请减少组分链数或每链规模后重试。");
    expect(error.parentElement).toBe(limits.parentElement);
    expect(error.closest(".np-polymer-md-system-limits")).toBeNull();
    expect(screen.queryByLabelText("链数")).toBeNull();
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("组分 A 链数"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("组分 B 链数"), { target: { value: "2" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText("组分 A 每链目标原子数") as HTMLInputElement).value).toBe("5000");
    expect(mocks.create).not.toHaveBeenCalled();
  });
  it("omits the environment and acceptance block while retaining the authoritative maintenance restriction", async () => {
    mocks.capabilities.mockResolvedValue({ ...caps, submit_enabled: false, worker_ready: false, admission_reason: "maintenance", maintenance: { enabled: true, persisted: true, updated_at: null }, runtime: { adapter_scientific_validation: "pending", accepted_protocols: [] }, acceptance_summary: { accepted_protocol_count: 0 }, resource_limits: { cpu_count: 16, memory_bytes: 32 * 1024 ** 3, stage_timeout_seconds: 86400, job_timeout_seconds: 345600 } });
    page(); await screen.findByText(/计算服务正在维护/);
    expect(screen.queryByText("运行环境与验收状态")).toBeNull();
    expect(screen.queryByText(/当前独立科学验收尚未核验通过/)).toBeNull();
    expect(screen.queryByText(/已验收协议/)).toBeNull();
    expect(screen.queryByText(/当前执行资源|单阶段最长|单任务最长/)).toBeNull();
    expect(screen.queryByText(/容量已满/)).toBeNull();
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);
  });
  it("does not expose historical runtime seals in configuration or change submission readiness", async () => {
    mocks.capabilities.mockResolvedValue({ ...caps, runtime: { scientific_acceptance: "passed", adapter_scientific_validation: "passed", accepted_protocols: ["legacy-global-approval"] } });
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText("运行环境与验收状态")).toBeNull();
    expect(screen.queryByText(/当前独立科学验收尚未核验通过/)).toBeNull();
    expect(screen.queryByText(/已验收协议/)).toBeNull();
    expect(screen.queryByText(/legacy-global-approval/)).toBeNull();
    expect(screen.queryByText(/当前适配器已记录科学验证/)).toBeNull();
  });
  it("supports keyboard navigation across all three workspaces", async () => {
    page(); const config = screen.getByRole("tab", { name: "模拟配置" }); config.focus();
    fireEvent.keyDown(config, { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "任务中心" }));
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("polymer-md-tab-tasks");
    fireEvent.keyDown(document.activeElement!, { key: "End" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "结果分析" }));
  });
  it("exposes scientific choices while platform timing controls and old catalogs are absent", async () => {
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByRole("combobox", { name: "聚合物力场" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "几何优化方法" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "原子电荷模型" })).toBeTruthy();
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
    expect(screen.getByRole("heading", { name: "平衡后自动分析 14 项默认包含" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "其他 RadonPy 计算流程" })).toBeNull();
    expect(screen.getByRole("group", { name: "聚合物类型" })).toBeTruthy();
    expect(screen.getAllByRole("radio")).toHaveLength(5);
    expect(screen.queryByRole("checkbox", { name: "密度" })).toBeNull();
    expect(screen.getByRole("checkbox", { name: "玻璃化转变温度 Tg" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "溶解度参数 SP / 内聚能密度 CED" })).toBeTruthy();
    for (const name of ["高级设置", "选择采样方案", "EQ3 生产采样时长", "每次追加平衡时长", "最多追加次数", "EQ3 / 追加热力学与轨迹输出间隔", "随机种子（可留空）", "热导采样时长", "传热方向复制倍数", "只读科学协议", "RadonPy 能力目录"]) expect(screen.queryByLabelText(name)).toBeNull();
    expect(screen.queryByRole("radio", { name: "基础物性" })).toBeNull();
    expect(screen.getByLabelText("平台运行方案").textContent).toContain("运行方案由平台统一管理");
    const forcefield = screen.getByRole("combobox", { name: "聚合物力场" });
    fireEvent.click(forcefield);
    for (const option of screen.getAllByRole("option") as HTMLButtonElement[]) expect(option.disabled).toBe(false);
    fireEvent.keyDown(forcefield, { key: "Escape" });
    expect(mocks.create).not.toHaveBeenCalled();
  });
  it("saves native forcefield and quantum choices across refresh and submits those exact choices", async () => {
    const first = page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    choose("聚合物力场", "GAFF2");
    choose("几何优化方法", "HF（Hartree–Fock）");
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false);
    expect(readPolymerMdUserConfiguration()).toMatchObject({ forcefield: "GAFF2", opt_method: "HF" });
    first.unmount(); page();
    expect(screen.getByRole("combobox", { name: "聚合物力场" }).textContent).toContain("GAFF2");
    expect(screen.getByRole("combobox", { name: "几何优化方法" }).textContent).toContain("HF（Hartree–Fock）");
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.calls[0][0]).toMatchObject({ forcefield: "GAFF2", native_configuration: { opt_method: "HF" } });
  });
  it.each(["alternating", "random", "block", "blend"])("persists and submits the complete %s model without using only its first monomer", async mode => {
    const first = page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("radio", { name: MODEL_LABELS[mode as keyof typeof MODEL_LABELS] }));
    fireEvent.change(screen.getByLabelText("单体 B 重复单元 SMILES"), { target: { value: "*COC*" } });
    fireEvent.change(screen.getByRole("textbox", { name: "重复单元 SMILES" }), { target: { value: "*CO*" } });
    first.unmount(); page();
    expect((screen.getByRole("radio", { name: MODEL_LABELS[mode as keyof typeof MODEL_LABELS] }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("单体 B 重复单元 SMILES") as HTMLTextAreaElement).value).toBe("*COC*");
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    const submitted = mocks.create.mock.calls[0][0];
    expect(submitted.smiles).toBe("*CO*");
    expect(submitted.model_configuration).toMatchObject({ mode, components: [{ id: "A", smiles: "*CO*" }, { id: "B", smiles: "*COC*" }] });
    if (mode === "random") expect(submitted.model_configuration.ratios).toEqual([.5, .5]);
    if (mode === "block") expect(submitted.model_configuration.blocks).toEqual([{ component_id: "A", dp: 10 }, { component_id: "B", dp: 10 }]);
    if (mode === "blend") expect(submitted.model_configuration.components.map((component: { n_chain: number }) => component.n_chain)).toEqual([5, 5]);
  });
  it("submits the automatic base analysis with no optional work selected", async () => {
    savePolymerMdUserConfiguration({ ...defaultPolymerMdUserConfiguration(), properties: [] });
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByLabelText("所选配置执行状态").textContent).toContain("默认统一分析 14 项基础物性。未选择追加计算。");
    expect((screen.getAllByRole("checkbox") as HTMLInputElement[]).every(input => !input.checked)).toBe(true);
    expect(screen.queryByRole("button", { name: "清空选择" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.calls[0][0].native_configuration.properties).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
    expect(mocks.create.mock.calls[0][0].calculation).toBe("basic");
  });
  it("includes the required thermal calculation when thermal diffusivity is requested", async () => {
    savePolymerMdUserConfiguration({ ...defaultPolymerMdUserConfiguration(), properties: [] });
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("checkbox", { name: "热扩散率" }));
    expect(screen.getByLabelText("所选配置执行状态").textContent).toContain("RNEMD 热导率");
    expect(screen.getByLabelText("传热方向")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.calls[0][0].native_configuration.properties).toEqual(
      [...POLYMER_MD_BASE_PROPERTIES, "thermal_conductivity", "thermal_diffusivity"].sort());
    expect(mocks.create.mock.calls[0][0].calculation).toBe("basic_tc");
  });
  it("preserves a v1 draft and removed choices until explicit repair and migration", async () => {
    savePolymerMdUserConfiguration({ ...defaultPolymerMdUserConfiguration(), opt_method: "b3lyp", properties: ["density", "temperature"] });
    const saved = JSON.parse(sessionStorage.getItem(POLYMER_MD_USER_CONFIGURATION_KEY)!);
    saved.configuration_version = 1;
    sessionStorage.setItem(POLYMER_MD_USER_CONFIGURATION_KEY, JSON.stringify(saved));
    const first = page();
    expect(screen.getByRole("combobox", { name: "几何优化方法" }).textContent).toContain("已保存的选择：b3lyp");
    expect(screen.getByRole("button", { name: "移除保存的性质 temperature" })).toBeTruthy();
    expect(readPolymerMdUserConfiguration().__configuration_version).toBe("1");
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);
    first.unmount(); page();
    const optimization = screen.getByRole("combobox", { name: "几何优化方法" });
    expect(optimization.textContent).toContain("已保存的选择：b3lyp");
    fireEvent.keyDown(optimization, { key: "ArrowDown" });
    const savedOption = within(screen.getByRole("listbox", { name: "几何优化方法" })).getByRole("option", { selected: true });
    expect(savedOption.textContent).toContain("已保存的选择：b3lyp");
    fireEvent.keyDown(optimization, { key: "Escape" });
    expect(readPolymerMdUserConfiguration().opt_method).toBe("b3lyp");
    fireEvent.keyDown(optimization, { key: "ArrowDown" });
    fireEvent.keyDown(optimization, { key: "ArrowDown" });
    fireEvent.keyDown(optimization, { key: "Enter" });
    expect(optimization.textContent).toContain("HF（Hartree–Fock）");
    fireEvent.click(screen.getByRole("button", { name: "移除保存的性质 temperature" }));
    fireEvent.click(screen.getByRole("button", { name: "已核对，迁移到当前配置版本" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(readPolymerMdUserConfiguration()).toMatchObject({ opt_method: "HF", properties: [...POLYMER_MD_BASE_PROPERTIES].sort() });
    expect(readPolymerMdUserConfiguration().__configuration_version).toBeUndefined();
    expect(mocks.create).not.toHaveBeenCalled();
  });
  it("uses the platform protocol instead of inheriting hidden timing values from an old draft", async () => {
    savePolymerMdDraft({ ...POLYMER_MD_DEFAULTS, eq_ns: 10, additional_ns: 12, max_additional: 0, sample_interval_ps: 2, tc_ns: 4, tc_rep: 5, seed: 999 });
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.calls[0][0]).toMatchObject({ eq_ns: POLYMER_MD_DEFAULTS.eq_ns, additional_ns: POLYMER_MD_DEFAULTS.additional_ns, max_additional: POLYMER_MD_DEFAULTS.max_additional, sample_interval_ps: POLYMER_MD_DEFAULTS.sample_interval_ps, tc_ns: POLYMER_MD_DEFAULTS.tc_ns, tc_rep: POLYMER_MD_DEFAULTS.tc_rep, seed: null });
  });
  it("recovers an old pending request unchanged without injecting the newly selected native methods", async () => {
    const original = { ...POLYMER_MD_DEFAULTS, eq_ns: 10, additional_ns: 12, max_additional: 0, sample_interval_ps: 2, tc_ns: 4, tc_rep: 5, seed: 999 };
    const pending = createPolymerMdPending(original);
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "恢复原提交" }) as HTMLButtonElement).disabled).toBe(false));
    choose("几何优化方法", "HF（Hartree–Fock）");
    expect(screen.getByText(/当前输入已更改/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "恢复原提交" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.calls[0][0]).toEqual(original);
    expect(mocks.create.mock.calls[0][1]).toBe(pending.key);
    expect(mocks.create.mock.calls[0][0]).not.toHaveProperty("native_configuration");
    expect(readPolymerMdUserConfiguration().opt_method).toBe("HF");
  });
  it("replays an old native QM-only pending request without adding the new default base package", async () => {
    const mapped = mapPolymerMdUserConfiguration(defaultPolymerMdUserConfiguration(), POLYMER_MD_DEFAULTS).request!;
    const original = { ...mapped, native_configuration: { ...mapped.native_configuration!, properties: ["qm_homo"] } };
    delete original.model_configuration;
    const pending = createPolymerMdPending(original);
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "恢复原提交" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByText(/当前输入已更改/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "恢复原提交" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce());
    expect(mocks.create.mock.calls[0][0]).toEqual(original);
    expect(mocks.create.mock.calls[0][1]).toBe(pending.key);
    expect(mocks.create.mock.calls[0][0].native_configuration.properties).toEqual(["qm_homo"]);
  });
  it("reports unsaved scientific choices and retries persistence without claiming they were saved", async () => {
    const originalSetItem = Storage.prototype.setItem;
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (key === POLYMER_MD_USER_CONFIGURATION_KEY) throw new DOMException("unavailable", "QuotaExceededError");
      originalSetItem.call(this, key, value);
    });
    try {
      page();
      choose("几何优化方法", "HF（Hartree–Fock）");
      expect(screen.getByText(/浏览器暂时无法保存所选配置/)).toBeTruthy();
      expect(screen.queryByText(/默认统一分析 14 项基础物性/)).toBeNull();
      expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);
      storage.mockRestore();
      fireEvent.click(screen.getByRole("button", { name: "重试保存配置" }));
      expect(screen.getByText(/默认统一分析 14 项基础物性/)).toBeTruthy();
      expect(readPolymerMdUserConfiguration().opt_method).toBe("HF");
      expect(mocks.create).not.toHaveBeenCalled();
    } finally { storage.mockRestore(); }
  });
  it("submits scientific parameters and records the actual job in the URL", async () => {
    page(); await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    const thermal = polymerMdUserConfigurationSpec.fields.find(field => field.id === "properties")!.options.find(option => option.value === "thermal_conductivity")!;
    fireEvent.click(screen.getByRole("checkbox", { name: thermal.label })); choose("传热方向", "z"); fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledOnce()); expect(mocks.create.mock.calls[0][0]).toMatchObject({ smiles: "*CC*", calculation: "basic_tc", tc_axis: "z", seed: null }); expect(mocks.create.mock.calls[0][1]).toEqual(expect.any(String));
    await screen.findByRole("heading", { name: "任务详情" }); expect(window.location.search).toContain(job().job_id); expect(screen.getByText("尚未评估")).toBeTruthy();
  });
  it("uses the same idempotency key when retrying an ambiguous submission", async () => {
    mocks.create.mockRejectedValueOnce(new Error("网络中断，请重试")).mockResolvedValueOnce(job()); page(); await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" })); await screen.findByText("网络中断，请重试"); fireEvent.click(screen.getByRole("button", { name: "恢复原提交" })); await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2)); expect(mocks.create.mock.calls[0][1]).toBe(mocks.create.mock.calls[1][1]);
  });
  it("persists before POST and recovers an accepted request after lost response and page reload", async () => {
    mocks.create.mockImplementationOnce(async (request, key) => {
      expect(JSON.parse(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)!)).toMatchObject({ key, request, scope: { owner: "test-user", session: "test-session", api: "http://localhost:3000/api/v1/polymer-md" } });
      throw new TypeError("响应丢失");
    }).mockResolvedValueOnce(job());
    const first = page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await screen.findByText("响应丢失");
    const originalKey = mocks.create.mock.calls[0][1];
    first.unmount();
    page();
    await screen.findByRole("button", { name: "恢复原提交" });
    fireEvent.change(screen.getByLabelText("目标温度"), { target: { value: "350" } });
    expect(screen.getByText(/当前输入已更改/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "恢复原提交" }));
    await screen.findByRole("heading", { name: "任务详情" });
    expect(mocks.create.mock.calls[1][1]).toBe(originalKey);
    expect(mocks.create.mock.calls[1][0].temp).toBe(300);
    expect(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "模拟配置" }));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(3));
    expect(mocks.create.mock.calls[2][1]).not.toBe(originalKey);
  });
  it("keeps an ambiguous submission across demo switching and requires explicit recovery even when old", async () => {
    mocks.create.mockRejectedValueOnce(new TypeError("响应丢失"));
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await screen.findByText("响应丢失");
    const stored = JSON.parse(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)!);
    stored.createdAt = Date.now() - POLYMER_MD_PENDING_MAX_AGE_MS - 1;
    sessionStorage.setItem(POLYMER_MD_PENDING_KEY, JSON.stringify(stored));
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + POLYMER_MD_PENDING_MAX_AGE_MS + 1);
    fireEvent.click(screen.getByRole("button", { name: "示例演示" }));
    fireEvent.click(screen.getByRole("button", { name: "正式计算" }));
    await screen.findByText(/已超过 24 小时/);
    expect(mocks.create).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "放弃恢复（不取消后台任务）" }));
    expect(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)).toBeNull();
    expect(mocks.cancel).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "恢复原提交" })).toBeNull();
  });
  it.each(["owner", "session", "api"])("never restores a pending key from another %s", async difference => {
    mocks.create.mockRejectedValueOnce(new TypeError("响应丢失"));
    const first = page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await screen.findByText("响应丢失");
    const key = mocks.create.mock.calls[0][1];
    first.unmount();
    if (difference === "api") {
      const record = JSON.parse(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)!);
      record.scope.api = "https://other.example/api/v1/polymer-md";
      sessionStorage.setItem(POLYMER_MD_PENDING_KEY, JSON.stringify(record));
    } else {
      const session = getSession();
      retireSession();
      installSession({ ...session, ...(difference === "owner" ? { user: { ...session.user!, id: "another-owner" } } : { session_id: "another-session" }) });
    }
    mocks.create.mockResolvedValueOnce(job());
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("button", { name: "恢复原提交" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[1][1]).not.toBe(key);
  });
  it("blocks POST when durable pending storage is unavailable", async () => {
    const originalSetItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) { if (key === POLYMER_MD_PENDING_KEY) throw new DOMException("storage unavailable", "QuotaExceededError"); originalSetItem.call(this, key, value); });
    try {
      page();
      await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
      await screen.findByText(/浏览器无法可靠保存待确认的提交/);
      expect(mocks.create).not.toHaveBeenCalled();
    } finally { vi.restoreAllMocks(); }
  });
  it("persists a conflict without silently replacing the key", async () => {
    mocks.create.mockRejectedValueOnce(new PolymerMdApiError(409, "提交标识冲突", "idempotency_conflict"));
    page();
    await waitFor(() => expect((screen.getByRole("button", { name: "提交正式计算" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交正式计算" }));
    await screen.findByText("提交标识冲突");
    expect(JSON.parse(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)!).conflict).toBe(true);
    expect((screen.getByRole("button", { name: "恢复原提交" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.create).toHaveBeenCalledOnce();
  });
  it("dismisses cancellation without a request and confirms deletion for the exact finished job", async () => {
    window.history.replaceState(null, "", `/md-simulation?job=${job().job_id}`);
    const view = page(); await screen.findByRole("heading", { name: "任务详情" });
    const cancel = screen.getByRole("button", { name: "取消任务" }); cancel.focus(); fireEvent.click(cancel);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "返回" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull(); expect(mocks.cancel).not.toHaveBeenCalled(); expect(document.activeElement).toBe(cancel);
    view.unmount();
    mocks.job.mockResolvedValue(job({ status: "completed" }));
    page(); await screen.findByRole("button", { name: "删除任务记录" });
    fireEvent.click(screen.getByRole("button", { name: "删除任务记录" })); expect(mocks.remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith(job().job_id, expect.anything()));
  });
  it("requires the same cancellation confirmation from task history", async () => {
    mocks.jobs.mockResolvedValue({ items: [job()], total: 1, page: 1, page_size: 10 });
    page(); fireEvent.click(screen.getByRole("tab", { name: "任务中心" }));
    fireEvent.click(await screen.findByRole("button", { name: "取消" })); expect(mocks.cancel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "返回" })); expect(mocks.cancel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "确认取消" }));
    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith(job().job_id, expect.anything()));
  });
  it("restores a job on refresh and waits for cancellation confirmation", async () => {
    window.history.replaceState(null, "", `/md-simulation?job=${job().job_id}`); page(); await screen.findByRole("heading", { name: "任务详情" });
    mocks.job.mockResolvedValue(job({ status: "cancel_requested" })); fireEvent.click(screen.getByRole("button", { name: "取消任务" })); expect(mocks.cancel).not.toHaveBeenCalled(); expect(screen.getByRole("alertdialog", { name: "确认取消任务" }).textContent).toContain(job().job_id); fireEvent.click(screen.getByRole("button", { name: "确认取消" })); await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith(job().job_id, expect.anything())); await screen.findByText("正在取消，等待进程退出"); expect(screen.queryByText("已取消")).toBeNull();
    mocks.job.mockResolvedValue(job({ status: "cancelled" })); fireEvent.click(screen.getByRole("button", { name: "刷新" })); await screen.findByText("已取消");
  });
  it("keeps execution completion distinct from unqualified scientific properties and cleans artifacts", async () => {
    const finished = job({ status: "completed", quality_status: "not_converged", results: { properties: [{ key: "Cp", value: null, unit: "J/(kg·K)", quality_status: "not_converged", reason: "有效样本不足" }, { key: "density", value: 1.125, unit: "g/cm³", quality_status: "passed", source_stage: "eq3", analysis_range: { start_ps: 2000, stop_ps: 5000 } }] }, artifacts: [{ artifact_id: "density", stage_run_id: stage.stage_run_id, filename: "density.csv", kind: "results", media_type: "text/csv", size_bytes: 200, sha256: "a".repeat(64) }] });
    mocks.job.mockResolvedValue(finished); window.history.replaceState(null, "", `/md-simulation?job=${finished.job_id}`); page(); await screen.findByText("执行完成"); expect(screen.getByText("尚未收敛")).toBeTruthy(); expect(screen.getByText("有效样本不足")).toBeTruthy(); expect(screen.getByText(/分析区间 2,000–5,000 ps/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "输出文件" }));
    fireEvent.click(screen.getByRole("button", { name: "下载 density.csv" })); await waitFor(() => expect(mocks.download).toHaveBeenCalledWith(finished.job_id, "density", "density.csv", 200, expect.any(AbortSignal)));
    mocks.job.mockResolvedValue({ ...finished, artifact_state: "deleted" }); fireEvent.click(screen.getByRole("button", { name: "清理输出文件" })); expect(mocks.clear).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name: "确认清理" })); await waitFor(() => expect(mocks.clear).toHaveBeenCalled()); await waitFor(() => expect(screen.queryByRole("button", { name: "下载 density.csv" })).toBeNull()); fireEvent.click(screen.getByRole("tab", { name: "概览" })); expect(screen.getByText("1.125")).toBeTruthy();
  });
  it("binds trajectory and distance calls to the selected actual stage and retires them on a stage change", async () => {
    const second = { ...stage, stage_run_id: "additional_1-1", stage: "additional_1" };
    const thermal = { ...stage, stage_run_id: "tc-1", stage: "tc" };
    mocks.job.mockResolvedValue(job({ stage_runs: [stage, second, thermal] })); mocks.trajectory.mockImplementation(async (id, sid, offset) => ({ job_id: id, stage_run_id: sid, total_frames: 2, frames: [{ frame_index: offset, time_ps: offset, points: [{ atom_id: 1, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 }, { atom_id: 2, chain_id: 1, atom_type: "H", x: 1, y: 0, z: 0 }], box: { lx: 10, ly: 10, lz: 10 } }] })); mocks.distance.mockImplementation(async (id, sid) => ({ job_id: id, stage_run_id: sid, frames: [0], time_ps: [0], distance: [1] }));
    window.history.replaceState(null, "", `/md-simulation?job=${job().job_id}`); page(); await screen.findByRole("heading", { name: "任务详情" }); fireEvent.click(screen.getByRole("tab", { name: "构象" })); await screen.findByText("真实坐标原子 1"); fireEvent.click(screen.getByRole("button", { name: "选择原子对" })); fireEvent.click(screen.getByRole("button", { name: "计算当前阶段距离" })); await screen.findByText("当前阶段距离已返回"); expect(mocks.distance.mock.calls[0].slice(0,3)).toEqual([job().job_id, stage.stage_run_id, { atom_id_1: 1, atom_id_2: 2, use_pbc: true, frame_count: 2 }]);
    expect(screen.getByText("轨迹来源：EQ3 平衡采样，第 1 帧")).toBeTruthy();
    choose("轨迹阶段", "第 1 次追加平衡 · 第 1 次"); await waitFor(() => expect(mocks.trajectory).toHaveBeenLastCalledWith(job().job_id, second.stage_run_id, 0, expect.anything())); expect(screen.queryByText("当前阶段距离已返回")).toBeNull();
    expect(await screen.findByText("轨迹来源：第 1 次追加平衡，第 1 帧")).toBeTruthy();
    choose("轨迹阶段", "RNEMD 热导率 · 第 1 次"); await waitFor(() => expect(mocks.trajectory).toHaveBeenLastCalledWith(job().job_id, thermal.stage_run_id, 0, expect.anything()));
    expect(await screen.findByText("轨迹来源：RNEMD 热导率，第 1 帧")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "下一帧" }));
    expect(await screen.findByText("轨迹来源：RNEMD 热导率，第 2 帧")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "距离使用周期边界" }));
    expect(screen.getByText("距离周期边界：关闭")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "选择原子对" })); fireEvent.click(screen.getByRole("button", { name: "计算当前阶段距离" })); await screen.findByText("当前阶段距离已返回");
    expect(mocks.distance.mock.calls.at(-1)?.slice(0, 3)).toEqual([job().job_id, thermal.stage_run_id, { atom_id_1: 1, atom_id_2: 2, use_pbc: false, frame_count: 2 }]);
  });
  it("renders the actual RNEMD temperature profile against position in angstroms", async () => {
    mocks.job.mockResolvedValue(job({ status: "completed", results: { series: [{ key: "temperature_profile", label: "steady temperature profile", unit: "K", points: [{ position_angstrom: 0, value: 290 }, { position_angstrom: 10, value: 310 }] }] } }));
    window.history.replaceState(null, "", `/md-simulation?job=${job().job_id}`); page();
    await screen.findByRole("tab", { name: "曲线" });
    fireEvent.click(screen.getByRole("tab", { name: "曲线" }));
    await screen.findByRole("img", { name: "稳态温度分布随位置变化曲线" });
    expect(screen.getByText("位置 (Å)")).toBeTruthy();
    expect(screen.queryByText("时间 (ps)")).toBeNull();
  });
  it("loads bounded real stage logs only when the user expands the stage", async () => {
    mocks.logs.mockResolvedValue({ job_id: job().job_id, stage_run_id: stage.stage_run_id, lines: ["Step Temp Density", "100 300 1.02"], truncated: true });
    window.history.replaceState(null, "", `/md-simulation?job=${job().job_id}`); page();
    await screen.findByRole("heading", { name: "任务详情" }); expect(mocks.logs).not.toHaveBeenCalled();
    const summary = screen.getByText("查看EQ3 平衡采样日志"), details = summary.closest("details")!;
    details.open = true; fireEvent(details, new Event("toggle"));
    await screen.findByText(/Step Temp Density/);
    expect(mocks.logs).toHaveBeenCalledWith(job().job_id, stage.stage_run_id, expect.any(AbortSignal));
    expect(screen.getByText(/更早内容已截断/)).toBeTruthy();
  });
  it("does not publish a delayed private job response after identity retirement", async () => {
    let resolve!: (j: PolymerMdJob) => void; mocks.job.mockReturnValue(new Promise<PolymerMdJob>(r => { resolve = r; })); window.history.replaceState(null, "", `/md-simulation?job=${job().job_id}`); page(); await waitFor(() => expect(mocks.job).toHaveBeenCalled());
    await act(async () => { retireSession(); resolve(job({ results: { density: 9.999 } })); await Promise.resolve(); }); expect(screen.queryByRole("heading", { name: "任务详情" })).toBeNull(); expect(screen.queryByText("9.999")).toBeNull();
  });
});
