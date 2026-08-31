// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchDrawerShell } from "./WorkbenchDrawerShell";

let containerWidth = 900;

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

function DrawerHarness() {
  const [open, setOpen] = useState(false);
  const [width, setWidth] = useState(380);
  return (
    <div className="np-structure-workbench">
      <button type="button" onClick={() => setOpen(true)}>运行预测</button>
      <WorkbenchDrawerShell
        open={open}
        hasRun
        width={width}
        title="性质预测结果"
        status="9 / 9 项已返回"
        headerIcon={<span />}
        reopenIcon={<span />}
        reopenLabel="展开预测结果"
        closeLabel="关闭性质预测结果"
        resizeLabel="调整性质预测结果抽屉宽度"
        onWidthChange={setWidth}
        onClose={() => setOpen(false)}
        onOpen={() => setOpen(true)}
      >
        <button type="button">第一个结果操作</button>
        <button type="button">最后一个结果操作</button>
      </WorkbenchDrawerShell>
    </div>
  );
}

beforeEach(() => {
  containerWidth = 900;
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
    width: containerWidth,
    height: 800,
    top: 0,
    right: containerWidth,
    bottom: 800,
    left: 0,
    x: 0,
    y: 0,
    toJSON: () => ({})
  }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("WorkbenchDrawerShell", () => {
  it("覆盖模式循环焦点，Escape 关闭并恢复触发器焦点", async () => {
    render(<DrawerHarness />);
    const trigger = screen.getByRole("button", { name: "运行预测" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "性质预测结果" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.querySelector(".np-sw-drawer-layer")?.classList.contains("is-overlay")).toBe(true);
    const close = screen.getByRole("button", { name: "关闭性质预测结果" });
    const last = screen.getByRole("button", { name: "最后一个结果操作" });
    await waitFor(() => expect(document.activeElement).toBe(close));

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("dialog", { name: "性质预测结果" })).toBeNull();
    expect(screen.getByRole("button", { name: "展开预测结果" })).toBeTruthy();
  });

  it("并排模式保持非模态并支持键盘调整宽度", async () => {
    containerWidth = 1400;
    render(<DrawerHarness />);
    const hiddenDialog = document.querySelector<HTMLElement>(".np-sw-drawer");
    await waitFor(() => expect(hiddenDialog?.getAttribute("aria-modal")).toBe("false"));

    const trigger = screen.getByRole("button", { name: "运行预测" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "性质预测结果" });
    expect(dialog.getAttribute("aria-modal")).toBe("false");
    expect(document.querySelector(".np-sw-drawer-layer")?.classList.contains("is-overlay")).toBe(false);
    expect(document.activeElement).toBe(trigger);

    const separator = screen.getByRole("separator", { name: "调整性质预测结果抽屉宽度" });
    expect(separator.getAttribute("aria-valuenow")).toBe("380");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("396");
  });
});
