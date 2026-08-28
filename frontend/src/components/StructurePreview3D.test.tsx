// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchStructure3D: vi.fn() }));

vi.mock("../services/api", () => ({ fetchStructure3D: mocks.fetchStructure3D }));

async function renderPreview(smiles: string) {
  const { StructurePreview3D } = await import("./StructurePreview3D");
  return render(<StructurePreview3D smiles={smiles} variant="bare" />);
}

function installViewer() {
  const viewer = {
    addModel: vi.fn(),
    setStyle: vi.fn(),
    zoomTo: vi.fn(),
    render: vi.fn(),
    clear: vi.fn(),
    setBackgroundColor: vi.fn()
  };
  window.$3Dmol = { createViewer: vi.fn(() => viewer) };
  return viewer;
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  document.getElementById("3dmol-script")?.remove();
  delete window.$3Dmol;
  mocks.fetchStructure3D.mockResolvedValue({ molblock: "mol", capped_smiles: "CC", format: "mol" });
});

afterEach(() => {
  cleanup();
  document.getElementById("3dmol-script")?.remove();
  delete window.$3Dmol;
});

describe("StructurePreview3D", () => {
  it("空结构不加载脚本也不请求构象", async () => {
    await renderPreview("");
    expect(document.getElementById("3dmol-script")).toBeNull();
    expect(mocks.fetchStructure3D).not.toHaveBeenCalled();
    expect(screen.getByText("暂无可预览结构")).not.toBeNull();
  });

  it("传递 AbortSignal、渲染构象并在卸载时清理 viewer", async () => {
    const viewer = installViewer();
    const view = await renderPreview("CC-3d-test");
    await waitFor(() => expect(mocks.fetchStructure3D).toHaveBeenCalledOnce());
    expect(mocks.fetchStructure3D.mock.calls[0]?.[1]).toBeInstanceOf(AbortSignal);
    await waitFor(() => expect(viewer.addModel).toHaveBeenCalledWith("mol", "mol"));

    view.unmount();
    expect(viewer.clear).toHaveBeenCalled();
    expect(viewer.render).toHaveBeenCalled();
  });

  it("脚本失败会移除节点，并允许按钮重新加载", async () => {
    await renderPreview("retry-3d-test");
    const failedScript = await waitFor(() => {
      const script = document.getElementById("3dmol-script") as HTMLScriptElement | null;
      if (!script) throw new Error("script pending");
      return script;
    });
    fireEvent.error(failedScript);
    expect(await screen.findByRole("button", { name: "重新加载 3D" })).not.toBeNull();
    expect(document.getElementById("3dmol-script")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重新加载 3D" }));
    const retryScript = await waitFor(() => {
      const script = document.getElementById("3dmol-script") as HTMLScriptElement | null;
      if (!script) throw new Error("retry script pending");
      return script;
    });
    expect(retryScript).not.toBe(failedScript);
    installViewer();
    fireEvent.load(retryScript);
    await waitFor(() => expect(mocks.fetchStructure3D).toHaveBeenCalledOnce());
  });

  it("卸载会取消尚未完成的构象请求", async () => {
    installViewer();
    const request: { signal: AbortSignal | null } = { signal: null };
    mocks.fetchStructure3D.mockImplementation((_smiles, signal: AbortSignal) => {
      request.signal = signal;
      return new Promise(() => undefined);
    });
    const view = await renderPreview("abort-3d-test");
    await waitFor(() => expect(request.signal).not.toBeNull());
    view.unmount();
    expect(request.signal?.aborted).toBe(true);
  });
});
