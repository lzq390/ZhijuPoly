// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MonomerMdProtocolCatalogResponse } from "../../types";
import { LibraryTestProvider, chooseCurrentStructure, testDocument } from "../../test/structureLibraryFixture";
import { StructureWorkspace } from "../../structure/workspace";
import { MonomerMdFormalConfig } from "./MonomerMdFormalConfig";

const config = {
  protocol: "Density" as const,
  temperature: 298,
  natoms: 1000,
  components: { A: 1 },
  smiles: { A: "CCO" },
  unknown: { nested: [1, { keep: true }] }
};

const catalog: MonomerMdProtocolCatalogResponse = {
  enabled: true,
  available: true,
  message: "ready",
  protocols: [{
    protocol: "Density",
    run_mode: "formal",
    supported: true,
    runtime_ready: true,
    default_config: config
  }]
};

function renderEditor(onApplyConfig = vi.fn(), workspace = new StructureWorkspace("CCC")) {
  render(
    <MonomerMdFormalConfig
      protocol="Density"
      config={config}
      catalog={catalog}
      canSubmit
      submissionReason="正式任务容量可用"
      isSubmitting={false}
      templateChangeCount={0}
      onProtocolChange={vi.fn()}
      onApplyConfig={onApplyConfig}
      onRestoreTemplate={vi.fn()}
      onKeepChangedTemplates={vi.fn()}
      onRestoreChangedTemplates={vi.fn()}
      onSubmit={vi.fn()}
    />, { wrapper: ({ children }) => <LibraryTestProvider workspace={workspace}>{children}</LibraryTestProvider> }
  );
  return onApplyConfig;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MonomerMdFormalConfig", () => {
  it("copies into one stable component row and requires explicit apply", async () => {
    const workspace = new StructureWorkspace("CCC");
    vi.spyOn(workspace, "captureForImport").mockResolvedValue(testDocument("CCN"));
    const apply = renderEditor(vi.fn(), workspace);
    fireEvent.click(screen.getByRole('button', { name: /添加组分/ }));
    fireEvent.change(screen.getByLabelText('组分 2 名称'), { target: { value: 'B' } });
    fireEvent.change(screen.getByLabelText('组分 2 摩尔配比'), { target: { value: '3' } });
    await chooseCurrentStructure(screen.getByRole('button', { name: '选择结构 · 组分 2' }));
    await waitFor(() => expect(screen.getByLabelText('组分 2 SMILES')).toHaveProperty('value', 'CCN'));
    expect(screen.getByLabelText('组分 1 SMILES')).toHaveProperty('value', 'CCO');
    expect(screen.getByLabelText('组分 2 名称')).toHaveProperty('value', 'B');
    expect(screen.getByLabelText('组分 2 摩尔配比')).toHaveProperty('value', '3');
    expect(apply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /应用表单修改/ }));
    expect(apply.mock.calls[0][1]).toMatchObject({ smiles: { A: 'CCO', B: 'CCN' }, components: { A: 1, B: 3 }, unknown: config.unknown });
  });

  it("does not apply a late result to a deleted row or its replacement", async () => {
    const workspace = new StructureWorkspace('CCC');
    let resolve!: (document: ReturnType<typeof testDocument>) => void;
    vi.spyOn(workspace, 'captureForImport').mockImplementation(() => new Promise(done => { resolve = done; }));
    renderEditor(vi.fn(), workspace);
    fireEvent.click(screen.getByRole('button', { name: /添加组分/ }));
    await chooseCurrentStructure(screen.getByRole('button', { name: '选择结构 · 组分 2' }));
    fireEvent.click(screen.getByRole('button', { name: '删除组分 2' }));
    fireEvent.click(screen.getByRole('button', { name: /添加组分/ }));
    await act(async () => resolve(testDocument('NN')));
    expect(screen.getByLabelText('组分 2 SMILES')).toHaveProperty('value', '');
    expect(screen.getByLabelText('组分 1 SMILES')).toHaveProperty('value', 'CCO');
  });

  it("uses protocol names in English and hides implementation-only managed paths", () => {
    renderEditor();
    expect(screen.getByRole("button", { name: /Density/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Transport/ })).toBeTruthy();
    expect(screen.queryByText("输运")).toBeNull();
    expect(screen.queryByText("系统托管路径")).toBeNull();
    expect(screen.queryByText("params_dir")).toBeNull();
    expect(screen.getByRole("heading", { name: "Density 配置" })).toBeTruthy();
    expect(screen.getByText("仅可提交当前已就绪的模拟类型")).toBeTruthy();
    expect(screen.getByText("每个组分名称需保持唯一，并填写对应配比和结构")).toBeTruthy();
  });

  it("applies a complete structured patch without rebuilding unknown data", () => {
    const onApply = renderEditor();
    fireEvent.change(screen.getByLabelText(/温度/), { target: { value: "310" } });
    fireEvent.click(screen.getByRole("button", { name: /应用表单修改/ }));
    expect(onApply).toHaveBeenCalledOnce();
    expect(onApply.mock.calls[0][1]).toMatchObject({
      temperature: 310,
      unknown: { nested: [1, { keep: true }] }
    });
  });

  it("keeps invalid JSON isolated from the last valid config", () => {
    const onApply = renderEditor();
    fireEvent.click(screen.getByRole("button", { name: /高级 JSON/ }));
    fireEvent.change(screen.getByLabelText("完整 MD 模拟高级 JSON"), { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: "应用 JSON" }));
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("JSON 无法解析");
  });

  it("requires an explicit discard decision before leaving unapplied JSON", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: /高级 JSON/ }));
    fireEvent.change(screen.getByLabelText("完整 MD 模拟高级 JSON"), { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: /结构化表单/ }));
    expect(screen.getByLabelText("完整 MD 模拟高级 JSON")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /结构化表单/ }));
    expect(screen.queryByLabelText("完整 MD 模拟高级 JSON")).toBeNull();
    expect(confirm).toHaveBeenCalledTimes(2);
  });

  it("blocks duplicate component names before canonical config changes", () => {
    const onApply = renderEditor();
    fireEvent.click(screen.getByRole("button", { name: /添加组分/ }));
    fireEvent.change(screen.getByLabelText("组分 2 名称"), { target: { value: "A" } });
    fireEvent.change(screen.getByLabelText("组分 2 SMILES"), { target: { value: "O" } });
    fireEvent.click(screen.getByRole("button", { name: /应用表单修改/ }));
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("组分名称必须唯一");
  });
});
