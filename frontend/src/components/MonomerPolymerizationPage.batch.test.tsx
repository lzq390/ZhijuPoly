// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MonomerPolymerizationStatusResponse, StructureWorkspaceContext } from "../types";
import { MonomerPolymerizationPage } from "./MonomerPolymerizationPage";

const api = vi.hoisted(() => ({ status: vi.fn(), preview: vi.fn() }));
vi.mock("../services/api", async (original) => ({
  ...await original<typeof import("../services/api")>(),
  fetchMonomerPolymerizationStatus: api.status,
  fetchStructure2D: api.preview
}));
const status: MonomerPolymerizationStatusResponse = {
  enabled: true, available: true, default_target_class: "polyimide",
  available_target_classes: ["polyimide"], max_results_limit: 20, message: "ready",
  batch: {
    enabled: true, available: true, message: "ready", formats: ["csv", "xlsx"],
    limits: { file_bytes: 10485760, request_bytes: 23068672, max_rows: 5000, max_pairs: 50000, retention_days: 7 }
  }
};
function showPage() {
  // This page consumes only the shared SMILES methods, independent of editor implementation.
  const structure = { smiles: "", setSmiles: vi.fn(), getCurrentSmiles: vi.fn().mockResolvedValue("") } as unknown as StructureWorkspaceContext;
  return render(<MonomerPolymerizationPage structure={structure} onEditStructure={vi.fn()} />);
}
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/monomer-polymerization");
  api.status.mockReset().mockResolvedValue(status);
  api.preview.mockReset().mockResolvedValue({ structure_svg: "<svg />" });
});
afterEach(cleanup);

describe("batch page integration", () => {
  it("keeps the default batch form and collapsed task window after a failed status refresh", async () => {
    showPage();
    const batch = await screen.findByRole("tab", { name: "批量聚合" });
    expect(batch.getAttribute("aria-selected")).toBe("true");
    const upload = screen.getByLabelText("上传单体表 A") as HTMLInputElement;
    const file = new File(["SMILES\nCCN"], "retained.csv", { type: "text/csv" });
    fireEvent.change(upload, { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "收起批量任务" }));
    api.status.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(screen.getByRole("button", { name: "重新检查 SMiPoly 是否可用" }));
    await screen.findByText("暂时无法连接 SMiPoly，请检查网络后重试。");
    expect(screen.getByRole("tab", { name: "批量聚合" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("上传单体表 A")).toBe(upload);
    expect(upload.files?.[0]).toBe(file);
    expect(screen.getByRole("button", { name: "展开批量任务" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新检查 SMiPoly 是否可用" }));
    await screen.findByText("准备就绪");
    expect(screen.getByLabelText("上传单体表 A")).toBe(upload);
  });

  it("keeps common copy and task collapse state when switching modes with the keyboard", async () => {
    const view = showPage();
    const batch = await screen.findByRole("tab", { name: "批量聚合" });
    const surfaceCopy = view.container.querySelector(".np-mp-surface-copy")?.textContent;
    const settingsCopy = view.container.querySelector("#np-mp-settings-title")?.parentElement?.textContent;
    const taskPanel = screen.getByRole("region", { name: "批量任务" });
    expect(taskPanel.closest("form")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "收起批量任务" }));
    fireEvent.keyDown(batch, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "单次聚合" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("button", { name: "聚合" })).toBeTruthy();
    expect(view.container.querySelector(".np-mp-surface-copy")?.textContent).toBe(surfaceCopy);
    expect(view.container.querySelector("#np-mp-settings-title")?.parentElement?.textContent).toBe(settingsCopy);
    fireEvent.keyDown(screen.getByRole("tab", { name: "单次聚合" }), { key: "Home" });
    expect(screen.getByRole("button", { name: "展开批量任务" })).toBeTruthy();
    expect(screen.queryByText(/候选预览/)).toBeNull();
  });

  it("honors single-mode navigation from the structure workbench", async () => {
    window.history.replaceState(null, "", "/monomer-polymerization?mode=single");
    showPage();
    await waitFor(() => expect(screen.getByRole("tab", { name: "单次聚合" }).getAttribute("aria-selected")).toBe("true"));
    expect(screen.getByRole("button", { name: "聚合" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "批量任务" })).toBeNull();
  });
});
