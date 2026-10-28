/* @vitest-environment jsdom */
import { useState, type ComponentProps } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeSourcesResponse } from "../../types";
import { ModalFocusSuspendedContext } from "../../hooks/useModalFocus";
import {
  DEFAULT_KNOWLEDGE_CONDITIONS,
  KnowledgeConditionsDialog,
  type KnowledgeConditionSettings,
} from "./KnowledgeConditionsDialog";

const readySources: KnowledgeSourcesResponse = {
  sources: [
    { data_source: "legacy", enabled: true, ready: true, total: 12 },
    { data_source: "extracted_literature", enabled: true, ready: true, total: 34 },
  ],
};

function defaults(): KnowledgeConditionSettings {
  return {
    ...DEFAULT_KNOWLEDGE_CONDITIONS,
    sources: [...DEFAULT_KNOWLEDGE_CONDITIONS.sources],
    filters: { ...DEFAULT_KNOWLEDGE_CONDITIONS.filters },
  };
}

function renderDialog(overrides: Partial<ComponentProps<typeof KnowledgeConditionsDialog>> = {}) {
  const props = {
    settings: defaults(),
    onApply: vi.fn(),
    onClose: vi.fn(),
    sources: readySources,
    sourceError: null,
    onRetrySources: vi.fn(),
    ...overrides,
  };
  return { ...render(<KnowledgeConditionsDialog {...props} />), props };
}

const checkbox = (name: string) => screen.getByRole("checkbox", { name }) as HTMLInputElement;
const year = (name: string) => screen.getByLabelText(name) as HTMLInputElement;
const applyButton = () => screen.getByRole("button", { name: "应用条件" }) as HTMLButtonElement;

afterEach(() => cleanup());

describe("knowledge condition settings dialog", () => {
  it("starts with the legacy source and 20 records and focuses its close button", async () => {
    renderDialog();
    expect(screen.getByRole("dialog", { name: "条件设置" }).getAttribute("aria-modal")).toBe("true");
    expect(checkbox("现有专利知识库").checked).toBe(true);
    expect(checkbox("提取文献库").checked).toBe(false);
    expect((screen.getByLabelText("每库每页数量") as HTMLSelectElement).value).toBe("20");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭条件设置" })));
  });

  it("discards edited drafts on cancel without mutating the saved settings", () => {
    const settings: KnowledgeConditionSettings = {
      ...defaults(), sources: ["legacy", "extracted_literature"],
      filters: { ...defaults().filters, year_from: "2020" },
    };
    const view = renderDialog({ settings });
    fireEvent.change(year("文献起始年份"), { target: { value: "2024" } });
    fireEvent.change(screen.getByLabelText("每库每页数量"), { target: { value: "100" } });
    fireEvent.click(checkbox("现有专利知识库"));
    fireEvent.click(checkbox("有性质信息"));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(view.props.onClose).toHaveBeenCalledTimes(1);
    expect(view.props.onApply).not.toHaveBeenCalled();
    expect(settings).toEqual({
      ...defaults(), sources: ["legacy", "extracted_literature"],
      filters: { ...defaults().filters, year_from: "2020" },
    });
    view.unmount();
    renderDialog({ settings });
    expect(year("文献起始年份").value).toBe("2020");
    expect(checkbox("现有专利知识库").checked).toBe(true);
    expect(checkbox("有性质信息").checked).toBe(false);
    expect((screen.getByLabelText("每库每页数量") as HTMLSelectElement).value).toBe("20");
  });

  it.each([50, 100] as const)("applies both sources, all literature filters and page size %i", (pageSize) => {
    const { props } = renderDialog();
    fireEvent.click(checkbox("提取文献库"));
    fireEvent.change(year("文献起始年份"), { target: { value: "1000" } });
    fireEvent.change(year("文献结束年份"), { target: { value: "3000" } });
    for (const name of ["有性质信息", "有工艺信息", "有可用原文"]) fireEvent.click(checkbox(name));
    fireEvent.change(screen.getByLabelText("每库每页数量"), { target: { value: String(pageSize) } });
    expect(applyButton().disabled).toBe(false);
    fireEvent.click(applyButton());
    expect(props.onApply).toHaveBeenCalledExactlyOnceWith({
      sources: ["legacy", "extracted_literature"], pageSize,
      filters: {
        year_from: "1000", year_to: "3000",
        has_properties: true, has_processes: true, has_original: true,
      },
    });
  });

  it("requires at least one source before applying", () => {
    const { props } = renderDialog();
    fireEvent.click(checkbox("现有专利知识库"));
    expect(applyButton().disabled).toBe(true);
    fireEvent.click(applyButton());
    expect(props.onApply).not.toHaveBeenCalled();
    fireEvent.click(checkbox("提取文献库"));
    expect(applyButton().disabled).toBe(false);
  });

  it.each([
    ["999", ""], ["3001", ""], ["", "999"], ["", "3001"], ["2025", "2020"],
  ])("rejects invalid literature year bounds %s to %s", (yearFrom, yearTo) => {
    const { props } = renderDialog({ settings: { ...defaults(), sources: ["extracted_literature"] } });
    fireEvent.change(year("文献起始年份"), { target: { value: yearFrom } });
    fireEvent.change(year("文献结束年份"), { target: { value: yearTo } });
    expect(applyButton().disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toBeTruthy();
    fireEvent.click(applyButton());
    expect(props.onApply).not.toHaveBeenCalled();
  });

  it("disables all literature fields when its source is unselected and clears only those filters", () => {
    const { props } = renderDialog({ settings: {
      ...defaults(), filters: {
        year_from: "2020", year_to: "2026",
        has_properties: true, has_processes: true, has_original: true,
      },
    } });
    for (const field of [
      year("文献起始年份"), year("文献结束年份"),
      checkbox("有性质信息"), checkbox("有工艺信息"), checkbox("有可用原文"),
    ]) expect(field.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "清空文献筛选" }));
    expect(year("文献起始年份").value).toBe("");
    expect(year("文献结束年份").value).toBe("");
    for (const name of ["有性质信息", "有工艺信息", "有可用原文"]) expect(checkbox(name).checked).toBe(false);
    expect(checkbox("现有专利知识库").checked).toBe(true);
    expect(checkbox("提取文献库").checked).toBe(false);
    fireEvent.click(applyButton());
    expect(props.onApply).toHaveBeenCalledExactlyOnceWith(defaults());
  });

  it.each([
    ["legacy", "现有专利知识库", false, true],
    ["legacy", "现有专利知识库", true, false],
    ["extracted_literature", "提取文献库", false, true],
    ["extracted_literature", "提取文献库", true, false],
  ] as const)("disables unavailable %s source %s (enabled=%s, ready=%s)", (source, label, enabled, ready) => {
    renderDialog({ sources: { sources: readySources.sources.map((item) => item.data_source === source ? { ...item, enabled, ready, reason: "Synthetic unavailable source" } : item) } });
    expect(checkbox(label).disabled).toBe(true);
  });

  it("retries source availability after a failed status check", () => {
    const { props } = renderDialog({ sources: null, sourceError: "Synthetic source status failure" });
    expect(screen.getByText("Synthetic source status failure")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新检查来源" }));
    expect(props.onRetrySources).toHaveBeenCalledTimes(1);
    expect(props.onApply).not.toHaveBeenCalled();
  });

  it("cancels only a backdrop click, without applying its draft", () => {
    const { props } = renderDialog();
    const dialog = screen.getByRole("dialog", { name: "条件设置" });
    fireEvent.click(dialog);
    expect(props.onClose).not.toHaveBeenCalled();
    fireEvent.click(dialog.parentElement!);
    expect(props.onClose).toHaveBeenCalledTimes(1);
    expect(props.onApply).not.toHaveBeenCalled();
  });

  it("removes a source from the draft if a later availability response marks it offline", () => {
    const { props, rerender } = renderDialog({ settings: { ...defaults(), sources: ["legacy", "extracted_literature"] } });
    rerender(<KnowledgeConditionsDialog {...props} sources={{ sources: readySources.sources.map((source) => source.data_source === "extracted_literature" ? { ...source, ready: false } : source) }} />);
    expect(checkbox("提取文献库").disabled).toBe(true);
    expect(checkbox("提取文献库").checked).toBe(false);
    fireEvent.click(applyButton());
    expect(props.onApply).toHaveBeenCalledExactlyOnceWith(defaults());
    expect(props.settings.sources).toEqual(["legacy", "extracted_literature"]);
  });

  it("owns modal focus and Escape even when background drawers are suspended", async () => {
    const onClose = vi.fn();
    render(<ModalFocusSuspendedContext.Provider value={true}><KnowledgeConditionsDialog settings={defaults()} sources={readySources} sourceError={null} onApply={() => {}} onClose={onClose} onRetrySources={() => {}} /></ModalFocusSuspendedContext.Provider>);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭条件设置" })));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("wraps forward and backward Tab focus inside the dialog", async () => {
    renderDialog();
    const dialog = screen.getByRole("dialog", { name: "条件设置" });
    const close = screen.getByRole("button", { name: "关闭条件设置" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    const controls = Array.from(dialog.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]',
    )).filter((element) => !element.closest('[hidden], [aria-hidden="true"]'));
    const last = controls.at(-1)!;
    last.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it.each(["escape", "close"] as const)("dismisses by %s and restores the trigger focus", async (method) => {
    const onClose = vi.fn();
    const onApply = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(false);
      return <>
        <button type="button" onClick={() => setOpen(true)}>打开条件设置</button>
        {open ? <KnowledgeConditionsDialog settings={defaults()} sources={readySources} sourceError={null}
          onApply={onApply} onRetrySources={() => {}} onClose={() => { onClose(); setOpen(false); }} /> : null}
      </>;
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "打开条件设置" });
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭条件设置" })));
    if (method === "escape") fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    else fireEvent.click(screen.getByRole("button", { name: "关闭条件设置" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onApply).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "条件设置" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});
