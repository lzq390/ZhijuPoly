// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSessionEpoch, GUEST_SESSION, installSession, retireSession } from "../auth/session";
import { clearPrivateWebStorage } from "../auth/storage";
import { StructureWorkspace } from "../structure/workspace";
import type { MonomerRetrosynthesisResponse, StructureWorkspaceContext } from "../types";
import { MonomerRetrosynthesisPage } from "./MonomerRetrosynthesisPage";
import { MONOMER_RETROSYNTHESIS_DRAFT_KEY, readRetrosynthesisDraft, saveRetrosynthesisDraft } from "./monomer-retrosynthesis/session";

const api = vi.hoisted(() => ({ predictMonomerPrecursors: vi.fn(), fetchStructure2D: vi.fn() }));
vi.mock("../services/api", async importOriginal => ({ ...await importOriginal<object>(), ...api }));

function structure(smiles = "CCO"): StructureWorkspaceContext {
  const workspace = new StructureWorkspace(smiles);
  return { workspace, smiles, setSmiles: workspace.setSmiles, getCurrentSmiles: vi.fn().mockResolvedValue(smiles) };
}
function result(total = 2): MonomerRetrosynthesisResponse {
  return { input_smiles: "CCO", canonical_smiles: "CCO", target_role: "auto", inferred_target_role: "other", query_time_ms: 12, total,
    candidates: Array.from({ length: total }, (_, i) => ({ rank: i + 1, raw_output: "C.CO", reactants_smiles: "C.CO",
      canonical_reactants_smiles: "C.CO", valid_smiles: i === 0, all_reactants_smaller_than_target: true,
      reaction_hint: `候选提示 ${i + 1}`, reactants: [{ input_smiles: "CO", canonical_smiles: "CO", valid_smiles: true, heavy_atom_count: 2 }] })) };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
const target = () => screen.getByRole("textbox", { name: "目标单体 SMILES" }) as HTMLTextAreaElement;
const run = () => fireEvent.click(screen.getByRole("button", { name: "运行反推" }));
function mount(shared = structure(), initialInput?: { smiles?: string; notice?: string }) {
  const onEditStructure = vi.fn();
  return { shared, onEditStructure, ...render(<MonomerRetrosynthesisPage structure={shared} onEditStructure={onEditStructure} initialInput={initialInput} />) };
}

beforeEach(() => {
  sessionStorage.clear();
  api.predictMonomerPrecursors.mockReset().mockResolvedValue(result());
  api.fetchStructure2D.mockReset().mockResolvedValue({ structure_svg: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"><path d="M10 50 L80 20"/></svg>' });
  Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn(() => ({ matches: false,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() })) });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("独立单体逆合成模块", () => {
  it("初始为空且不探测服务，不因共享结构变化自动覆盖目标", () => {
    const view = mount();
    expect(target().value).toBe("");
    expect(screen.queryByRole("dialog", { name: "单体反推结果" })).toBeNull();
    expect(api.predictMonomerPrecursors).not.toHaveBeenCalled();
    expect(api.fetchStructure2D).not.toHaveBeenCalled();
    fireEvent.change(target(), { target: { value: "CCN" } });
    view.rerender(<MonomerRetrosynthesisPage structure={structure("CCCC")} onEditStructure={view.onEditStructure} />);
    expect(target().value).toBe("CCN");
  });

  it("恢复草稿，快捷输入仅覆盖目标，编辑共享结构前保存参数", () => {
    saveRetrosynthesisDraft({ smiles: "CC", targetRole: "diamine", returnCount: "7" }, getSessionEpoch());
    const view = mount(structure(), { smiles: "CCO" });
    expect(target().value).toBe("CCO");
    expect((screen.getByLabelText("反推候选数") as HTMLInputElement).value).toBe("7");
    expect(screen.getByRole("combobox").textContent).toContain("二胺");
    fireEvent.click(screen.getByRole("button", { name: "编辑共享结构" }));
    expect(view.onEditStructure).toHaveBeenCalledOnce();
    view.unmount(); mount();
    expect(target().value).toBe("CCO");
    expect(readRetrosynthesisDraft().returnCount).toBe("7");
  });

  it("损坏草稿回退默认值，空共享结构和同步失败保留目标", async () => {
    sessionStorage.setItem(MONOMER_RETROSYNTHESIS_DRAFT_KEY, "bad json");
    const shared = structure(""); mount(shared);
    expect(target().value).toBe("");
    fireEvent.change(target(), { target: { value: "CC" } });
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    await screen.findByText("当前共享结构为空，已保留反推草稿。");
    expect(target().value).toBe("CC");
    vi.mocked(shared.getCurrentSmiles).mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    await screen.findByText("读取共享结构失败，已保留反推草稿。请重试。");
    expect(target().value).toBe("CC");
  });

  it("主动导入会生成预览，异步导入不覆盖期间的新输入", async () => {
    const shared = structure(); mount(shared);
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    await waitFor(() => expect(target().value).toBe("CCO"));
    await screen.findByAltText("目标单体结构");
    const pending = deferred<string>();
    vi.mocked(shared.getCurrentSmiles).mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    fireEvent.change(target(), { target: { value: "CCN" } });
    await act(async () => pending.resolve("CCCC"));
    expect(target().value).toBe("CCN");
  });

  it("未挂载画板时也可导入已经接受的共享文档", async () => {
    const shared = structure("");
    shared.workspace.commitSmiles("CCO");
    vi.mocked(shared.getCurrentSmiles).mockResolvedValue("CCO");
    mount(shared);
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    await waitFor(() => expect(target().value).toBe("CCO"));
    await screen.findByAltText("目标单体结构");
  });

  it("失焦生成预览，输入变化隐藏旧图并取消旧请求，失败可以重试", async () => {
    mount(); fireEvent.change(target(), { target: { value: "CCO" } });
    expect(api.fetchStructure2D).not.toHaveBeenCalled();
    fireEvent.blur(target()); await screen.findByAltText("目标单体结构");
    const pending = deferred<{ structure_svg: string }>();
    api.fetchStructure2D.mockReturnValueOnce(pending.promise);
    fireEvent.change(target(), { target: { value: "CCN" } });
    expect(screen.queryByAltText("目标单体结构")).toBeNull();
    fireEvent.blur(target());
    const signal = api.fetchStructure2D.mock.calls.at(-1)?.[1] as AbortSignal;
    fireEvent.change(target(), { target: { value: "CCCC" } });
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ structure_svg: "<svg/>" }));
    expect(screen.queryByAltText("目标单体结构")).toBeNull();
    api.fetchStructure2D.mockRejectedValueOnce(new Error("预览暂不可用"));
    fireEvent.blur(target()); await screen.findByText("预览暂不可用");
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
    await screen.findByAltText("目标单体结构");
  });

  it.each(["", "0", "11", "1.5"])("候选数 %s 无效时不提交", count => {
    mount(); run(); expect(screen.getByText("请输入目标单体的 SMILES。")).toBeTruthy();
    fireEvent.change(target(), { target: { value: "CCO" } });
    fireEvent.change(screen.getByLabelText("反推候选数"), { target: { value: count } }); run();
    expect(screen.getByText("候选数必须是 1–10 的整数。")).toBeTruthy();
    expect(api.predictMonomerPrecursors).not.toHaveBeenCalled();
  });

  it.each([1, 3, 5, 10])("候选数 %s 保持 API 参数及 AbortSignal", async count => {
    mount(); fireEvent.click(screen.getByRole("button", { name: "加载示例" }));
    fireEvent.change(screen.getByLabelText("反推候选数"), { target: { value: String(count) } }); run();
    await screen.findByText("候选提示 1");
    expect(api.predictMonomerPrecursors).toHaveBeenCalledWith({ smiles: "C=C(C)C(=O)OC", target_role: "auto",
      num_beams: Math.max(5, count), num_return_sequences: count, max_new_tokens: 128 }, expect.any(AbortSignal));
  });

  it("结果支持类型标注、合法性、翻页、复制和参数过期提示", async () => {
    api.predictMonomerPrecursors.mockResolvedValue({ ...result(), target_role: "diamine", inferred_target_role: "diamine" });
    mount(structure(), { smiles: "CCO" }); run(); await screen.findByText("用户指定类型");
    expect(screen.getByText("候选 1 · 合法 SMILES")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看下一个候选" }));
    expect(screen.getByText("候选 2 · 需人工校验")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看下一个候选" }));
    expect(screen.getByText("候选提示 1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "复制前体 1 SMILES" }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("CO");
    fireEvent.change(target(), { target: { value: "CCN" } });
    expect(screen.getByText("参数已修改，当前结果对应上次提交的参数。")).toBeTruthy();
  });

  it("接口失败和空结果可回到参数重试，收起重开不会重复请求", async () => {
    api.predictMonomerPrecursors.mockRejectedValueOnce(new Error("服务暂不可用"));
    mount(structure(), { smiles: "CCO" }); run(); await screen.findByText("服务暂不可用");
    fireEvent.click(screen.getByRole("button", { name: "调整反推参数" }));
    await waitFor(() => expect(document.activeElement).toBe(target()));
    api.predictMonomerPrecursors.mockResolvedValueOnce(result(0)); run(); await screen.findByText("未找到可展示候选");
    fireEvent.click(screen.getByRole("button", { name: "关闭单体反推结果" }));
    fireEvent.click(await screen.findByRole("button", { name: "展开反推结果" }));
    expect(api.predictMonomerPrecursors).toHaveBeenCalledTimes(2);
  });

  it("重复提交取消旧请求、忽略迟到结果，卸载取消当前请求", async () => {
    const first = deferred<MonomerRetrosynthesisResponse>(), second = deferred<MonomerRetrosynthesisResponse>();
    api.predictMonomerPrecursors.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const view = mount(structure(), { smiles: "CCO" }); run();
    const firstSignal = api.predictMonomerPrecursors.mock.calls[0][1];
    fireEvent.change(target(), { target: { value: "CCN" } }); run();
    expect(firstSignal.aborted).toBe(true);
    await act(async () => second.resolve({ ...result(), canonical_smiles: "CCN" }));
    await act(async () => first.resolve(result(0)));
    expect(screen.getByText("候选提示 1")).toBeTruthy();
    api.predictMonomerPrecursors.mockReturnValue(new Promise(() => {})); run();
    const thirdSignal = api.predictMonomerPrecursors.mock.calls[2][1];
    view.unmount(); expect(thirdSignal.aborted).toBe(true);
  });

  it("访客浏览与导入不请求服务，点击运行才触发登录", async () => {
    installSession(GUEST_SESSION);
    const login = vi.fn(); window.addEventListener("nexpoly:login-required", login);
    const view = mount();
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    await waitFor(() => expect(target().value).toBe("CCO")); fireEvent.blur(target());
    expect(api.fetchStructure2D).not.toHaveBeenCalled();
    expect(login).not.toHaveBeenCalled(); run();
    expect(login).toHaveBeenCalledOnce(); expect(api.predictMonomerPrecursors).not.toHaveBeenCalled();
    view.unmount(); window.removeEventListener("nexpoly:login-required", login);
  });

  it("换号清理草稿，并阻止旧身份的导入、结果及缓存写回", async () => {
    const pending = deferred<MonomerRetrosynthesisResponse>(), imported = deferred<string>();
    api.predictMonomerPrecursors.mockReturnValueOnce(pending.promise);
    const shared = structure(); vi.mocked(shared.getCurrentSmiles).mockReturnValueOnce(imported.promise);
    mount(shared, { smiles: "CCO" }); run();
    fireEvent.click(screen.getByRole("button", { name: "使用当前结构" }));
    const oldEpoch = getSessionEpoch();
    act(() => { retireSession(); clearPrivateWebStorage(); installSession(GUEST_SESSION); });
    await act(async () => { pending.resolve(result()); imported.resolve("CCN"); });
    expect(screen.queryByText("候选提示 1")).toBeNull(); expect(target().value).toBe("CCO");
    saveRetrosynthesisDraft({ smiles: "old", targetRole: "auto", returnCount: "5" }, oldEpoch);
    expect(sessionStorage.getItem(MONOMER_RETROSYNTHESIS_DRAFT_KEY)).toBeNull();
  });
});
