// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MdDemoAtomDistanceResponse } from "../../types";
import type { PolymerMdArtifact, PolymerMdCapabilities, PolymerMdDistance, PolymerMdJob, PolymerMdStage, PolymerMdTrajectory } from "../../types/polymerMd";
import { clearPolymerMdViewState } from "../../services/polymerMdViewState";
import { installSession, retireSession } from "../../auth/session";
import { PolymerMdResults } from "./PolymerMdResults";
import { POLYMER_MD_DEFAULTS } from "./config";

const api = vi.hoisted(() => ({ trajectory: vi.fn(), distance: vi.fn(), logs: vi.fn(), download: vi.fn(), bundle: vi.fn() }));
vi.mock("../../services/polymerMdApi", () => ({
  fetchPolymerMdTrajectory: api.trajectory, calculatePolymerMdDistance: api.distance,
  fetchPolymerMdStageLogs: api.logs, downloadPolymerMdArtifact: api.download
}));
vi.mock("../../services/polymerMdArtifactBundle", () => ({ downloadPolymerMdArtifacts: api.bundle }));
vi.mock("../md-simulation/MdSeriesChart", () => ({ MdSeriesChart: () => <div>科学曲线</div> }));
vi.mock("../md-simulation/MdTrajectoryExplorer", () => ({ MdTrajectoryExplorer: ({ stageLabel, frameLabel, timePs, points, selections, distance, distanceLoading, distanceEnabled, distanceUsePbc, onSelectionChange, onCalculate }: {
  stageLabel: string; frameLabel: string; timePs: number; points: Array<{ atom_id: number; chain_id: number; atom_type: string }>;
  selections: Array<{ atom_id: number } | null>; distance: MdDemoAtomDistanceResponse | null;
  distanceLoading: boolean; distanceEnabled: boolean; distanceUsePbc: boolean;
  onSelectionChange: (selection: unknown) => void; onCalculate: () => void;
}) => <div>
  <p aria-label="当前构象">{stageLabel} / {frameLabel} / {timePs} ps / {points.length} 个原子</p>
  <p aria-label="已选原子">{selections.map(atom => atom?.atom_id ?? "未选").join("/")}</p>
  <p aria-label="距离周期边界">{distanceUsePbc ? "开启" : "关闭"}</p>
  <button onClick={() => onSelectionChange(points.slice(0, 2).map(({ atom_id, chain_id, atom_type }) => ({ atom_id, chain_id, atom_type })))}>选取原子对</button>
  <button disabled={!distanceEnabled || distanceLoading || selections.some(atom => !atom)} onClick={onCalculate}>读取距离</button>
  {distanceLoading ? <p role="status">正在分析距离</p> : null}
  {distance ? <output aria-label="距离结果">{distance.distance.join(" / ")}</output> : null}
</div> }));

const JOB_ID = "98f12c00-8194-4ae2-bd35-140cac6ff992";
function stage(name: string): PolymerMdStage {
  return { stage_run_id: `${name}-1`, stage: name, attempt: 1, status: "completed", quality_status: "passed", results: {}, quality: {}, errors: [] };
}
function artifact(stageId = "eq3-1", overrides: Partial<PolymerMdArtifact> = {}): PolymerMdArtifact {
  return { artifact_id: `${stageId}-xtc`, stage_run_id: stageId, kind: "trajectory", filename: "trajectory.xtc", media_type: "application/octet-stream", size_bytes: 4096, sha256: "a".repeat(64), ...overrides };
}
function job(overrides: Partial<PolymerMdJob> = {}): PolymerMdJob {
  return { job_id: JOB_ID, status: "completed", quality_status: "passed", scientific_request: { ...POLYMER_MD_DEFAULTS, seed: 1729 }, stage: "analyze_0",
    stage_runs: [stage("eq3")], artifacts: [artifact()], results: {}, created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T01:00:00Z", artifact_state: "available", ...overrides };
}
function trajectory(stageId: string, offset = 0): PolymerMdTrajectory {
  return { job_id: JOB_ID, stage_run_id: stageId, total_frames: 4, frames: [{ frame_index: offset, time_ps: offset * 10,
    points: [{ atom_id: 320, chain_id: 1, atom_type: "C", x: offset, y: 0, z: 0 }, { atom_id: 1253, chain_id: 2, atom_type: "H", x: offset + 1, y: 0, z: 0 }], box: { lx: 10, ly: 10, lz: 10 } }] };
}
function distance(stageId = "eq3-1", values = [1.2, 1.1, 1, .9]): PolymerMdDistance {
  return { job_id: JOB_ID, stage_run_id: stageId, frames: [0, 1, 2, 3], time_ps: [0, 10, 20, 30], distance: values,
    sampling: { source_frame_count: 4, start_frame: 0, stop_frame: 4, stride: 1, sampled_frames: 4, first_frame: 0, last_frame: 3, start_ps: 0, stop_ps: 30 } };
}
const capabilities: PolymerMdCapabilities = { enabled: true, submit_enabled: false, schema_ready: true, worker_ready: true, protocol_version: 1,
  supported_forcefields: ["GAFF2_mod"], supported_terminals: ["CH3"], supported_calculations: ["basic"], supported_tacticities: ["atactic"], trajectory_available: true, distance_available: true,
  analysis: { status: "ready", timeout_seconds: 90 }, limits: { per_owner_active: 1, global_active: 3, execution: 1, max_total_atoms: 20000, max_rnemd_atoms: 60000 } };
function result(value: PolymerMdJob) {
  return <PolymerMdResults job={value} capabilities={capabilities} serviceCheck="confirmed" busy={null} onCancel={vi.fn()} onClear={vi.fn()} onDelete={vi.fn()} />;
}
async function openConformation() {
  fireEvent.click(screen.getByRole("tab", { name: "构象" }));
  await screen.findByLabelText("当前构象");
}
function selectStage(label: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "轨迹阶段" }));
  fireEvent.click(within(screen.getByRole("listbox", { name: "轨迹阶段" })).getByText(label, { selector: "strong", exact: true }).closest('[role="option"]')!);
}
async function calculateDistance() {
  fireEvent.click(screen.getByRole("button", { name: "选取原子对" }));
  fireEvent.click(screen.getByRole("button", { name: "读取距离" }));
  await screen.findByLabelText("距离结果");
}

beforeEach(() => {
  clearPolymerMdViewState();
  vi.resetAllMocks();
  api.trajectory.mockImplementation(async (_id: string, stageId: string, offset: number) => trajectory(stageId, offset));
  api.distance.mockImplementation(async (_id: string, stageId: string) => distance(stageId));
});
afterEach(() => { cleanup(); clearPolymerMdViewState(); });

describe("polymer MD view persistence", () => {
  it("retries a failed trajectory explicitly while polls and restored successful views avoid extra reads", async () => {
    const value = job();
    api.trajectory.mockRejectedValueOnce(new Error("轨迹临时不可读取"));
    const mounted = render(result(value));
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    await screen.findByText("轨迹临时不可读取");

    mounted.rerender(result({ ...value, updated_at: "2026-10-29T02:00:00Z" }));
    expect(api.trajectory).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "重试读取轨迹" }));
    await screen.findByLabelText("当前构象");
    expect(api.trajectory).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "重试读取轨迹" })).toBeNull();
    expect(screen.queryByText("轨迹临时不可读取")).toBeNull();
    mounted.rerender(result({ ...value, updated_at: "2026-10-29T03:00:00Z" }));
    mounted.unmount();
    render(result(value));
    await screen.findByLabelText("当前构象");
    expect(api.trajectory).toHaveBeenCalledTimes(2);
  });

  it("aborts a retry when its source changes and never replaces the new source with a late response", async () => {
    const value = job();
    let finishRetry!: (value: PolymerMdTrajectory) => void;
    api.trajectory.mockRejectedValueOnce(new Error("轨迹临时不可读取"))
      .mockImplementationOnce(() => new Promise<PolymerMdTrajectory>(resolve => { finishRetry = resolve; }));
    const mounted = render(result(value));
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    await screen.findByText("轨迹临时不可读取");
    fireEvent.click(screen.getByRole("button", { name: "重试读取轨迹" }));
    const signal = api.trajectory.mock.calls[1][3] as AbortSignal;
    expect(screen.queryByRole("button", { name: "重试读取轨迹" })).toBeNull();

    const current = { ...value, artifacts: [artifact("eq3-1", { sha256: "b".repeat(64) })] };
    mounted.rerender(result(current));
    await screen.findByLabelText("当前构象");
    expect(signal.aborted).toBe(true);
    expect(api.trajectory).toHaveBeenCalledTimes(3);
    const stale = trajectory("eq3-1");
    stale.frames[0].time_ps = 999;
    await act(async () => { finishRetry(stale); await Promise.resolve(); });
    expect(screen.getByLabelText("当前构象").textContent).toContain(" / 0 ps /");
    mounted.unmount();
    render(result(current));
    await screen.findByLabelText("当前构象");
    expect(screen.getByLabelText("当前构象").textContent).toContain(" / 0 ps /");
    expect(api.trajectory).toHaveBeenCalledTimes(3);
  });

  it("retires an owner's pending retry before another session restores the same task ID", async () => {
    const value = job();
    let finishRetry!: (value: PolymerMdTrajectory) => void;
    api.trajectory.mockRejectedValueOnce(new Error("轨迹临时不可读取"))
      .mockImplementationOnce(() => new Promise<PolymerMdTrajectory>(resolve => { finishRetry = resolve; }));
    const mounted = render(result(value));
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    await screen.findByText("轨迹临时不可读取");
    fireEvent.click(screen.getByRole("button", { name: "重试读取轨迹" }));
    const signal = api.trajectory.mock.calls[1][3] as AbortSignal;
    act(() => {
      retireSession();
      installSession({ authenticated: true, user: { id: "next-owner", username: "next", must_change_password: false },
        session_id: "next-owner-session", csrf_token: "next-owner-csrf", capabilities: {} });
    });
    expect(signal.aborted).toBe(true);
    mounted.unmount();
    render(result(value));
    await openConformation();
    const stale = trajectory("eq3-1");
    stale.frames[0].time_ps = 999;
    await act(async () => { finishRetry(stale); await Promise.resolve(); });
    expect(screen.getByLabelText("当前构象").textContent).toContain(" / 0 ps /");
    expect(api.trajectory).toHaveBeenCalledTimes(3);
    expect(api.distance).not.toHaveBeenCalled();
  });

  it.each([403, 404])("retires an unavailable distance source (%s) before returning to the view", async status => {
    const value = job(); render(result(value));
    await openConformation(); await calculateDistance();
    api.distance.mockRejectedValueOnce(Object.assign(new Error("该阶段不可再访问"), { status }));
    fireEvent.click(screen.getByRole("button", { name: "读取距离" }));
    await screen.findByText("该阶段不可再访问");
    expect(screen.queryByLabelText("当前构象")).toBeNull();
    expect(screen.queryByLabelText("距离结果")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "曲线" }));
    await openConformation();
    expect(screen.queryByLabelText("距离结果")).toBeNull();
    expect(screen.getByLabelText("已选原子").textContent).toBe("未选/未选");
    expect(api.trajectory).toHaveBeenCalledTimes(2); expect(api.distance).toHaveBeenCalledTimes(2);
  });

  it("keeps the chosen stage, frame, atoms, PBC and distance when returning from curves", async () => {
    render(result(job({ stage_runs: [stage("eq1"), stage("eq3")], artifacts: [artifact("eq1-1"), artifact()] })));
    await openConformation();
    selectStage("EQ3 平衡采样 · 第 1 次");
    await waitFor(() => expect(screen.getByLabelText("当前构象").textContent).toContain("EQ3 平衡采样"));
    fireEvent.click(screen.getByRole("button", { name: "下一帧" }));
    await waitFor(() => expect(screen.getByLabelText("当前构象").textContent).toContain("第 2 帧 / 10 ps"));
    fireEvent.click(screen.getByRole("checkbox", { name: "距离使用周期边界" }));
    await calculateDistance();
    const reads = api.trajectory.mock.calls.length;

    fireEvent.click(screen.getByRole("tab", { name: "曲线" }));
    expect(screen.queryByLabelText("当前构象")).toBeNull();
    await openConformation();
    expect(screen.getByRole("combobox", { name: "轨迹阶段" }).textContent).toContain("EQ3 平衡采样");
    expect(screen.getByLabelText("当前构象").textContent).toContain("第 2 帧 / 10 ps");
    expect(screen.getByLabelText("已选原子").textContent).toBe("320/1253");
    expect((screen.getByRole("checkbox", { name: "距离使用周期边界" }) as HTMLInputElement).checked).toBe(false);
    expect(screen.getByLabelText("距离结果").textContent).toBe("1.2 / 1.1 / 1 / 0.9");
    expect(screen.getByLabelText("原子距离采样范围").textContent).toContain("实际 4 个采样点");
    expect(api.trajectory).toHaveBeenCalledTimes(reads);
    expect(api.distance).toHaveBeenCalledOnce();
    expect(api.distance.mock.calls[0][2]).toEqual({ atom_id_1: 320, atom_id_2: 1253, use_pbc: false, frame_count: 4 });
  });

  it("restores the conformation tab and validated results after unmount without rereading or resubmitting", async () => {
    const value = job();
    const mounted = render(result(value));
    await openConformation();
    await calculateDistance();
    mounted.unmount();

    render(result({ ...value, updated_at: "2026-10-29T02:00:00Z" }));
    await screen.findByLabelText("当前构象");
    expect(screen.getByRole("tab", { name: "构象" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("已选原子").textContent).toBe("320/1253");
    expect(screen.getByLabelText("距离结果").textContent).toBe("1.2 / 1.1 / 1 / 0.9");
    expect(api.trajectory).toHaveBeenCalledOnce();
    expect(api.distance).toHaveBeenCalledOnce();
  });

  it("aborts a pending distance on unmount and never caches its late response over the last success", async () => {
    const value = job();
    const mounted = render(result(value));
    await openConformation();
    await calculateDistance();
    let finish!: (value: PolymerMdDistance) => void;
    api.distance.mockImplementationOnce(() => new Promise<PolymerMdDistance>(resolve => { finish = resolve; }));
    fireEvent.click(screen.getByRole("button", { name: "读取距离" }));
    expect(screen.getByText("正在分析距离")).toBeTruthy();
    const signal = api.distance.mock.calls[1][3] as AbortSignal;
    mounted.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { finish(distance("eq3-1", [99, 99, 99, 99])); await Promise.resolve(); });

    render(result(value));
    await screen.findByLabelText("当前构象");
    expect(screen.queryByText("正在分析距离")).toBeNull();
    expect(screen.getByLabelText("距离结果").textContent).toBe("1.2 / 1.1 / 1 / 0.9");
    expect((screen.getByRole("button", { name: "读取距离" }) as HTMLButtonElement).disabled).toBe(false);
    expect(api.trajectory).toHaveBeenCalledOnce();
    expect(api.distance).toHaveBeenCalledTimes(2);
  });

  it("retains a view for unrelated publications but retires it when its scientific source changes", async () => {
    const value = job();
    const mounted = render(result(value));
    await openConformation();
    await calculateDistance();
    const published = { ...value, updated_at: "2026-10-29T02:00:00Z", artifacts: [...value.artifacts,
      artifact("eq2-1"), artifact("eq3-1", { artifact_id: "eq3-engine-log", kind: "log", filename: "engine.log" })] };
    mounted.rerender(result(published));
    expect(screen.getByLabelText("已选原子").textContent).toBe("320/1253");
    expect(screen.getByLabelText("距离结果")).toBeTruthy();
    expect(api.trajectory).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("tab", { name: "曲线" }));
    await openConformation();
    expect(screen.getByLabelText("距离结果")).toBeTruthy();
    expect(api.trajectory).toHaveBeenCalledOnce();

    let finish!: (value: PolymerMdTrajectory) => void;
    api.trajectory.mockImplementationOnce(() => new Promise<PolymerMdTrajectory>(resolve => { finish = resolve; }));
    mounted.rerender(result({ ...published, artifacts: published.artifacts.map(item => item.artifact_id === "eq3-1-xtc" ? { ...item, sha256: "b".repeat(64), size_bytes: 8192 } : item) }));
    await waitFor(() => expect(api.trajectory).toHaveBeenCalledTimes(2));
    expect(screen.queryByLabelText("距离结果")).toBeNull();
    expect(screen.getByText("正在读取当前任务的轨迹…")).toBeTruthy();
    await act(async () => { finish(trajectory("eq3-1")); });
    await screen.findByLabelText("当前构象");
    expect(screen.getByLabelText("已选原子").textContent).toBe("未选/未选");
    expect(screen.queryByLabelText("距离结果")).toBeNull();
    expect(api.distance).toHaveBeenCalledOnce();
  });
});
