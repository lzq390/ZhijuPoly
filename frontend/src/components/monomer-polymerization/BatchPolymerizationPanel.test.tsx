// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BatchPolymerizationPanel } from "./BatchPolymerizationPanel";
import { BATCH_HISTORY_KEY } from "../../hooks/usePolymerizationBatchJob";
import { BatchArtifactError } from "../../services/polymerizationBatchApi";
import type { BatchImportPreview, BatchJob } from "../../types/polymerizationBatch";
import type { MonomerPolymerizationStatusResponse } from "../../types";

const mocks = vi.hoisted(() => ({ upload: vi.fn(), preview: vi.fn(), submit: vi.fn(), job: vi.fn(), results: vi.fn(), cancel: vi.fn(), download: vi.fn(), structure: vi.fn() }));
vi.mock("../../services/polymerizationBatchApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../services/polymerizationBatchApi")>(),
  batchUrl: (path: string) => `/api/v1/monomer-polymerization/batch${path}`,
  uploadBatchTables: mocks.upload, previewBatchTables: mocks.preview, submitBatchJob: mocks.submit,
  fetchBatchJob: mocks.job, fetchBatchResults: mocks.results, cancelBatchJob: mocks.cancel, downloadBatchArtifact: mocks.download
}));
vi.mock("../../services/api", () => ({ fetchStructure2D: mocks.structure }));
const id = "a".repeat(32);
const mapping = { sheet: null, encoding: "utf-8-sig" as const, smiles_column: "SMILES", id_column: "id", name_column: null };
const table = { headers: ["id", "SMILES"], sheets: [], sheet: null, row_count: 1, blank_rows: 0, sample: [{ id: "001", SMILES: "CC" }], mapping };
const tableStats = { row_count: 1, valid_rows: 1, unique_count: 1, duplicate_rows: 0, invalid_rows: 0, blank_rows: 0 };
const preview: BatchImportPreview = {
  import_id: "b".repeat(32), expires_at: "2026-09-11T00:00:00Z",
  files: { a: { filename: "a.csv", format: "csv", size_bytes: 10, sha256: "a" }, b: { filename: "b.csv", format: "csv", size_bytes: 10, sha256: "b" } },
  tables: { a: table, b: table }, preview_revision: "c".repeat(32), can_submit: true,
  statistics: { raw_pairs: 1, valid_pairs: 1, unique_pairs: 1, tables: { a: tableStats, b: tableStats } }, input_errors: [], input_error_count: 0
};
const job: BatchJob = {
  job_id: id, status: "queued", stage: "queued", target_class: "polyimide",
  summary: { ...preview.statistics!, processed_pairs: 0, computed_unique_pairs: 0, candidate_count: 0 },
  artifacts: {}, created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z", finished_at: null, expires_at: null, error_code: null, message: null
};
function completedJob(): BatchJob {
  return { ...job, status: "completed", stage: "finished", finished_at: new Date().toISOString(), expires_at: new Date(Date.now() + 7 * 86400000).toISOString(),
    artifacts: Object.fromEntries(["results.zip", "results.xlsx", "input_errors.csv"].map((name) => [name, { name, url: "unused", size_bytes: 100, media_type: "application/octet-stream", sha256: "a" }])) };
}
function navigateTask(jobId: string) {
  act(() => {
    window.history.pushState(null, "", `/monomer-polymerization?mode=batch&job_id=${jobId}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}
function mockFileSave() {
  const createObjectURL = vi.fn().mockReturnValue("blob:batch-result");
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = createObjectURL;
    static revokeObjectURL = revokeObjectURL;
  });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  return { createObjectURL, revokeObjectURL, click };
}
const status: MonomerPolymerizationStatusResponse = {
  enabled: true, available: true, default_target_class: "polyimide", available_target_classes: ["polyimide", "all"], max_results_limit: 20, message: "ready",
  batch: { enabled: true, available: true, formats: ["csv", "xlsx"], message: "ready", limits: { file_bytes: 10485760, request_bytes: 23068672, max_rows: 5000, max_pairs: 50000, retention_days: 7 } }
};
function page() {
  return render(
    <BatchPolymerizationPanel status={status} target="polyimide">
      {({ inputs, settings, taskPanel }) => <>{inputs}{settings}{taskPanel}</>}
    </BatchPolymerizationPanel>
  );
}
function files() {
  fireEvent.change(screen.getByLabelText("上传单体表 A"), { target: { files: [new File(["SMILES\nCC"], "a.csv")] } });
  fireEvent.change(screen.getByLabelText("上传单体表 B"), { target: { files: [new File(["SMILES\nCO"], "b.csv")] } });
}
beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/monomer-polymerization?mode=batch");
  mocks.upload.mockReset().mockResolvedValue(preview);
  mocks.preview.mockReset().mockResolvedValue(preview);
  mocks.submit.mockReset().mockResolvedValue(job);
  mocks.job.mockReset().mockResolvedValue(job);
  mocks.results.mockReset().mockResolvedValue({ items: [], total: 0, next_offset: null });
  mocks.cancel.mockReset().mockResolvedValue({ ...job, status: "cancelling" });
  mocks.download.mockReset().mockResolvedValue(new Blob(["results"], { type: "application/zip" }));
  mocks.structure.mockReset();
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("batch polymerization", () => {
  it("uploads, maps, validates and submits once, preserving the task link", async () => {
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await screen.findByLabelText("预检结果");
    expect(screen.queryByRole("combobox", { name: /SMILES 列/ })).toBeNull();
    expect(screen.getByRole("button", { name: "导入设置与前 20 行" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByLabelText("表 A 导入摘要").textContent).toContain("1 条有效 · 0 条错误");
    fireEvent.click(screen.getByRole("button", { name: "收起批量任务" }));
    fireEvent.click(screen.getByRole("button", { name: "开始批量聚合" }));
    await screen.findByText("等待计算");
    expect(screen.getByRole("button", { name: "收起批量任务" }).getAttribute("aria-expanded")).toBe("true");
    expect(mocks.submit).toHaveBeenCalledWith(preview.import_id, preview.preview_revision, "polyimide", expect.any(String), expect.any(AbortSignal));
    expect(localStorage.getItem(BATCH_HISTORY_KEY)).toContain(id);
    expect(window.location.search).toContain(`job_id=${id}`);
    expect((screen.getByRole("button", { name: "任务已提交" }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.structure).not.toHaveBeenCalled();
  });
  it("invalidates a preview on a mapping or file change", async () => {
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await screen.findByLabelText("预检结果");
    fireEvent.click(screen.getByRole("button", { name: "导入设置与前 20 行" }));
    const tableA = within(screen.getByRole("region", { name: "单体表 A 导入设置" }));
    fireEvent.click(tableA.getByRole("combobox", { name: "A SMILES 列（必填）" }));
    fireEvent.click(tableA.getByRole("option", { name: "id" }));
    expect(screen.queryByLabelText("预检结果")).toBeNull();
    expect((screen.getByRole("button", { name: "开始批量聚合" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("上传单体表 A"), { target: { files: [new File(["SMILES\nNN"], "new.csv")] } });
    expect(screen.queryByRole("combobox", { name: "A SMILES 列（必填）" })).toBeNull();
  });
  it("opens and closes both import settings and input tables together without losing mappings", async () => {
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await screen.findByLabelText("预检结果");
    const toggle = screen.getByRole("button", { name: "导入设置与前 20 行" });
    fireEvent.click(toggle);
    for (const role of ["A", "B"]) {
      expect(screen.getByRole("combobox", { name: `${role} SMILES 列（必填）` })).toBeTruthy();
      expect(screen.getByRole("table", { name: `单体表 ${role} 前 20 行` })).toBeTruthy();
    }
    fireEvent.click(screen.getByRole("combobox", { name: "A 编号列" }));
    fireEvent.click(screen.getByRole("option", { name: /不使用/ }));
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("combobox", { name: /SMILES 列/ })).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByRole("combobox", { name: "A 编号列" }).textContent).toContain("不使用");
    expect(screen.getByRole("combobox", { name: "B 编号列" }).textContent).toContain("id");
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getByText("上次预检的前 20 行")).toBeTruthy();
  });
  it("opens settings for an unrecognized SMILES column and accepts a corrected mapping", async () => {
    const unresolved = {
      ...preview, can_submit: false, statistics: null, preview_revision: null,
      tables: { ...preview.tables, a: { ...table, headers: ["id", "structure"], mapping: { ...mapping, smiles_column: null } } }
    };
    mocks.upload.mockResolvedValue(unresolved);
    mocks.preview.mockResolvedValue(unresolved);
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    const picker = await screen.findByRole("combobox", { name: "A SMILES 列（必填）" });
    await waitFor(() => expect((picker as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByRole("combobox", { name: "B SMILES 列（必填）" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "导入设置与前 20 行" }).getAttribute("aria-expanded")).toBe("true");
    expect((screen.getByRole("button", { name: "开始批量聚合" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("option", { name: "structure" }));
    mocks.preview.mockResolvedValue({ ...preview, tables: { ...preview.tables, a: { ...unresolved.tables.a, mapping: { ...mapping, smiles_column: "structure" } } } });
    fireEvent.click(screen.getByRole("button", { name: "重新预检" }));
    await screen.findByLabelText("预检结果");
    expect(mocks.preview).toHaveBeenLastCalledWith(preview.import_id, {
      a: { ...mapping, smiles_column: "structure" }, b: mapping
    }, expect.any(AbortSignal));
    expect((screen.getByRole("button", { name: "开始批量聚合" }) as HTMLButtonElement).disabled).toBe(false);
  });
  it("opens settings for a decoding error and rechecks with the selected encoding", async () => {
    const unreadable = {
      ...preview, can_submit: false, statistics: null, preview_revision: null,
      tables: { ...preview.tables, a: { ...table, error: "CSV 编码不匹配", headers: [], sample: [], mapping: { ...mapping, smiles_column: null, id_column: null } } }
    };
    mocks.upload.mockResolvedValue(unreadable);
    mocks.preview.mockResolvedValue(unreadable);
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    const picker = await screen.findByRole("combobox", { name: "A CSV 编码" });
    await waitFor(() => expect((picker as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(picker);
    fireEvent.click(screen.getByRole("option", { name: /GB18030/ }));
    mocks.preview.mockResolvedValue(preview);
    fireEvent.click(screen.getByRole("button", { name: "重新预检" }));
    await screen.findByLabelText("预检结果");
    expect(mocks.preview).toHaveBeenLastCalledWith(preview.import_id, {
      a: { sheet: null, encoding: "gb18030", smiles_column: null, id_column: null, name_column: null }, b: mapping
    }, expect.any(AbortSignal));
    expect(screen.queryByText("CSV 编码不匹配")).toBeNull();
  });
  it("does not apply an old upload response after replacing a file", async () => {
    let resolve!: (value: BatchImportPreview) => void;
    mocks.upload.mockReturnValue(new Promise((done) => { resolve = done; }));
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    fireEvent.change(screen.getByLabelText("上传单体表 A"), { target: { files: [new File(["SMILES\nNN"], "new.csv")] } });
    resolve(preview);
    await waitFor(() => expect((mocks.upload.mock.calls[0][2] as AbortSignal).aborted).toBe(true));
    expect(mocks.preview).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("预检结果")).toBeNull();
  });
  it("retries submission with the same idempotency key after a network error", async () => {
    mocks.submit.mockRejectedValueOnce(new Error("network failure")).mockResolvedValue(job);
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await screen.findByLabelText("预检结果");
    fireEvent.click(screen.getByRole("button", { name: "开始批量聚合" }));
    await screen.findByText("network failure");
    fireEvent.click(screen.getByRole("button", { name: "开始批量聚合" }));
    await screen.findByText("等待计算");
    expect(mocks.submit.mock.calls[0][3]).toBe(mocks.submit.mock.calls[1][3]);
  });
  it("restores completion and downloads without fetching or rendering candidate previews", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue({
      ...completedJob(),
      summary: { ...job.summary, processed_pairs: 1, computed_unique_pairs: 1, candidate_count: 24, complete: true, pair_statuses: { success: 1 } },
      artifacts: Object.fromEntries(["results.zip", "results.xlsx", "input_errors.csv"].map((name) => [name, { name, url: "unused", size_bytes: 100, media_type: "application/octet-stream", sha256: "a" }]))
    });
    page();
    const task = within(screen.getByRole("region", { name: "批量任务" }));
    await task.findByText("已完成");
    expect(task.getByText("已处理 1 / 1 个有效行组合。")).toBeTruthy();
    expect(task.getByText("24")).toBeTruthy();
    expect(task.getByText("一组单体可生成多个候选，候选结果数可能超过组合数。")).toBeTruthy();
    expect(task.getByText(/结束时间/)).toBeTruthy();
    expect(task.getByText(/文件到期时间/)).toBeTruthy();
    expect(task.getAllByRole("button", { name: /^下载/ })).toHaveLength(3);
    expect(task.queryByRole("table")).toBeNull();
    expect(task.queryByText(/候选预览/)).toBeNull();
    expect(task.queryByRole("button", { name: /查看结构|上一页|下一页/ })).toBeNull();
    expect(mocks.results).not.toHaveBeenCalled();
    expect(mocks.structure).not.toHaveBeenCalled();
    fireEvent.click(task.getByRole("button", { name: "收起批量任务" }));
    const expand = task.getByRole("button", { name: "展开批量任务" });
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    expect(task.getByText("已完成")).toBeTruthy();
    expect(task.getByText(/已处理 1 \/ 1/)).toBeTruthy();
    expect(task.queryByRole("button", { name: /^下载/ })).toBeNull();
    expect(task.queryByRole("combobox", { name: "最近任务" })).toBeNull();
    fireEvent.click(expand);
    expect(task.getAllByRole("button", { name: /^下载/ })).toHaveLength(3);
    expect(task.getByText("24")).toBeTruthy();
    expect(mocks.cancel).not.toHaveBeenCalled();
  });
  it("continues polling and updates the collapsed task summary", async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValueOnce(job).mockResolvedValue({
      ...job, status: "completed", stage: "finished",
      summary: { ...job.summary, processed_pairs: 1, computed_unique_pairs: 1, candidate_count: 2 }
    });
    page();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "收起批量任务" }));
    expect(screen.getByText("等待计算")).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(mocks.job).toHaveBeenCalledTimes(2);
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.getByText(/已处理 1 \/ 1/)).toBeTruthy();
    expect(screen.queryByText("候选结果数")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开批量任务" }));
    expect(screen.getByText("2")).toBeTruthy();
    expect(mocks.cancel).not.toHaveBeenCalled();
  });
  it("restores a cancelled task, shows partial results clearly, and does not cancel on unmount", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue({ ...job, status: "cancelled", stage: "finished", summary: { ...job.summary, complete: false }, artifacts: { "results.zip": { name: "results.zip", url: "unused", size_bytes: 100, media_type: "application/zip", sha256: "a" } } });
    const view = page();
    await screen.findByText("已取消");
    expect(screen.getByText(/此任务未完整完成/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "下载 CSV 结果包" }) as HTMLButtonElement).disabled).toBe(false);
    expect(mocks.results).not.toHaveBeenCalled();
    view.unmount();
    expect(mocks.cancel).not.toHaveBeenCalled();
  });
  it("shows refresh feedback while the request is pending and clears it after success or failure", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    const completed = { ...job, status: "completed" as const, stage: "finished" };
    mocks.job.mockResolvedValue(completed);
    page();
    await screen.findByText("已完成");
    let resolve!: (value: BatchJob) => void;
    mocks.job.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    const pending = await screen.findByRole("button", { name: "刷新中…" });
    expect(pending.getAttribute("aria-busy")).toBe("true");
    expect((pending as HTMLButtonElement).disabled).toBe(true);
    expect(pending.querySelector(".np-sw-spin")).not.toBeNull();
    resolve({ ...completed, summary: { ...completed.summary, candidate_count: 4 } });
    await screen.findByText("4");
    const refresh = screen.getByRole("button", { name: "刷新状态" });
    expect(refresh.getAttribute("aria-busy")).toBe("false");
    expect((refresh as HTMLButtonElement).disabled).toBe(false);
    mocks.job.mockRejectedValueOnce(new Error("无法刷新任务状态"));
    fireEvent.click(refresh);
    await screen.findByText("无法刷新任务状态");
    expect((screen.getByRole("button", { name: "刷新状态" }) as HTMLButtonElement).disabled).toBe(false);
  });
  it("requests cancellation without treating it as immediate completion", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    page();
    await screen.findByText("等待计算");
    mocks.job.mockResolvedValue({ ...job, status: "cancelling" });
    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith(id, expect.any(AbortSignal)));
    await waitFor(() => expect((screen.getByRole("button", { name: "正在取消" }) as HTMLButtonElement).disabled).toBe(true));
  });

  it("invalidates the previous preview before a replacement upload and only submits the new import after recovery", async () => {
    page(); files();
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    await screen.findByLabelText("预检结果");
    const next = { ...preview, import_id: "d".repeat(32), preview_revision: "e".repeat(32) };
    let resolve!: (value: BatchImportPreview) => void;
    mocks.upload.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    mocks.preview.mockRejectedValueOnce(new Error("新预检失败"));
    fireEvent.click(screen.getByRole("button", { name: "上传并预检" }));
    expect(screen.queryByLabelText("预检结果")).toBeNull();
    resolve(next);
    await screen.findByText("新预检失败");
    const start = screen.getByRole("button", { name: "开始批量聚合" }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    fireEvent.click(start);
    expect(mocks.submit).not.toHaveBeenCalled();
    mocks.preview.mockResolvedValueOnce(next);
    fireEvent.click(screen.getByRole("button", { name: "重新预检" }));
    await screen.findByLabelText("预检结果");
    fireEvent.click(start);
    await screen.findByText("等待计算");
    expect(mocks.submit).toHaveBeenCalledWith(next.import_id, next.preview_revision, "polyimide", expect.any(String), expect.any(AbortSignal));
  });

  it("clears the old task immediately on history navigation and does not offer cancellation for a failed replacement", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    page();
    await screen.findByText(id);
    const secondId = "b".repeat(32);
    let reject!: (reason: Error) => void;
    mocks.job.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    navigateTask(secondId);
    expect(screen.queryByText(id)).toBeNull();
    expect(screen.queryByRole("button", { name: "取消任务" })).toBeNull();
    expect(screen.queryByText("等待计算")).toBeNull();
    reject(new Error("新任务读取失败"));
    await screen.findByText("新任务读取失败");
    expect(screen.queryByRole("button", { name: "取消任务" })).toBeNull();
    mocks.job.mockResolvedValue({ ...job, job_id: secondId });
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await screen.findByText(secondId);
    mocks.cancel.mockResolvedValue({ ...job, job_id: secondId, status: "cancelling" });
    fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith(secondId, expect.any(AbortSignal)));
    expect(mocks.cancel).toHaveBeenCalledTimes(1);
  });

  it("remembers a successfully opened task link and restores it without a job_id", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue(completedJob());
    const first = page();
    await screen.findByText("已完成");
    expect(JSON.parse(localStorage.getItem(BATCH_HISTORY_KEY)!)).toEqual([id]);
    first.unmount();
    window.history.replaceState(null, "", "/monomer-polymerization?mode=batch");
    page();
    await screen.findByText(id);
    expect(mocks.job).toHaveBeenLastCalledWith(id, expect.any(AbortSignal));
    expect(screen.getByRole("combobox", { name: "最近任务" }).textContent).toContain(id.slice(0, 12));
  });

  it("keeps cancellation progress and failure inside the task, including while collapsed or files are replaced", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    let reject!: (reason: Error) => void;
    mocks.cancel.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    page();
    const task = within(screen.getByRole("region", { name: "批量任务" }));
    await task.findByText("等待计算");
    fireEvent.click(task.getByRole("button", { name: "取消任务" }));
    expect(task.getByRole("status").textContent).toBe("正在请求取消…");
    files();
    expect((mocks.cancel.mock.calls[0][1] as AbortSignal).aborted).toBe(false);
    expect((screen.getByRole("button", { name: "上传并预检" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(task.getByRole("button", { name: "收起批量任务" }));
    expect(task.getByRole("status").textContent).toBe("正在请求取消…");
    reject(new Error("取消服务暂时不可用"));
    await task.findByText("取消失败：取消服务暂时不可用");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    fireEvent.click(task.getByRole("button", { name: "展开批量任务" }));
    expect(task.getByRole("alert").textContent).toBe("取消失败：取消服务暂时不可用");
    expect((task.getByRole("button", { name: "取消任务" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it.each(["cancel", "download"] as const)("ignores a late %s response after switching tasks", async (kind) => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    const fileSave = mockFileSave();
    mocks.job.mockResolvedValue(kind === "cancel" ? job : completedJob());
    let reject!: (reason: Error) => void;
    mocks[kind].mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    page();
    await screen.findByText(id);
    fireEvent.click(screen.getByRole("button", { name: kind === "cancel" ? "取消任务" : "下载 CSV 结果包" }));
    const signal = mocks[kind].mock.calls[0].at(-1) as AbortSignal;
    const secondId = "b".repeat(32);
    mocks.job.mockResolvedValue({ ...completedJob(), job_id: secondId });
    navigateTask(secondId);
    await screen.findByText(secondId);
    expect(signal.aborted).toBe(true);
    await act(async () => { reject(new Error("旧任务请求失败")); });
    const task = within(screen.getByRole("region", { name: "批量任务" }));
    expect(task.queryByRole("alert")).toBeNull();
    expect(fileSave.createObjectURL).not.toHaveBeenCalled();
  });

  it("shows a failed download in the task and saves a file only after a successful retry", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue(completedJob());
    const fileSave = mockFileSave();
    let reject!: (reason: Error) => void;
    mocks.download.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    page();
    const download = await screen.findByRole("button", { name: "下载 CSV 结果包" });
    fireEvent.click(download);
    expect((download as HTMLButtonElement).disabled).toBe(true);
    expect(download.getAttribute("aria-busy")).toBe("true");
    reject(new Error("网络连接中断"));
    const task = within(screen.getByRole("region", { name: "批量任务" }));
    await task.findByText("下载失败：网络连接中断");
    expect(fileSave.createObjectURL).not.toHaveBeenCalled();
    expect((download as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(download);
    await task.findByText("已开始下载 results.zip。");
    expect(mocks.download).toHaveBeenLastCalledWith(id, "results.zip", expect.any(AbortSignal));
    expect(fileSave.click).toHaveBeenCalledTimes(1);
    expect((fileSave.click.mock.instances[0] as HTMLAnchorElement).download).toBe("results.zip");
  });

  it("handles a server expiry response by removing downloads and exposing the error while collapsed", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue(completedJob());
    mocks.download.mockRejectedValueOnce(new BatchArtifactError(410, "文件已过期。", "expired"));
    page();
    fireEvent.click(await screen.findByRole("button", { name: "下载 CSV 结果包" }));
    await screen.findByText("文件已过期");
    expect(screen.queryByRole("button", { name: /^下载/ })).toBeNull();
    expect(screen.getByText("下载失败：文件已过期。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起批量任务" }));
    expect(screen.getByText("文件已过期")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe("下载失败：文件已过期。");
  });

  it("disables only the missing artifact and allows a refresh to retry", async () => {
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue(completedJob());
    mocks.download.mockRejectedValueOnce(new BatchArtifactError(410, "结果文件缺失。", "artifact_missing"));
    page();
    const csv = await screen.findByRole("button", { name: "下载 CSV 结果包" });
    fireEvent.click(csv);
    await screen.findByText("下载失败：结果文件缺失。");
    expect((csv as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "下载 Excel" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("已完成")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新状态" }).getAttribute("aria-busy")).toBe("false"));
    expect((csv as HTMLButtonElement).disabled).toBe(false);
  });

  it("expires completed downloads on time even while the task is collapsed and no longer polling", async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${id}`);
    mocks.job.mockResolvedValue({ ...completedJob(), expires_at: new Date(Date.now() + 1000).toISOString() });
    page();
    await act(async () => {});
    expect(screen.getByRole("button", { name: "下载 Excel" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起批量任务" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByText("文件已过期")).toBeTruthy();
    expect(mocks.job).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "展开批量任务" }));
    expect(screen.queryByRole("button", { name: /^下载/ })).toBeNull();
    expect(screen.getByText("任务文件已过期，无法下载。请重新提交批量任务。")).toBeTruthy();
  });
});
