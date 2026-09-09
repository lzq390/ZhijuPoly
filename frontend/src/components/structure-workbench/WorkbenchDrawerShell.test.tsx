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

function DrawerHarness({ overlayContainerWidth }: { overlayContainerWidth?: number } = {}) {
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
        overlayContainerWidth={overlayContainerWidth}
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
  it("拖宽中关闭会清理拖动，并恢复正常退场而不是瞬间回中", async () => {
    containerWidth = 1400;
    render(<DrawerHarness />);
    fireEvent.click(screen.getByRole("button", { name: "运行预测" }));
    const drawer = screen.getByRole("dialog", { name: "性质预测结果" });
    await waitFor(() => expect(drawer.dataset.motionPhase).toBe("open"));
    const separator = screen.getByRole("separator");
    fireEvent.pointerDown(separator, { button: 0, pointerId: 1, clientX: 1000 });
    expect(document.querySelector(".np-sw-drawer-layer.is-resizing")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "关闭性质预测结果" }));
    expect(document.querySelector(".np-sw-drawer-layer.is-resizing")).toBeNull();
    expect(drawer.dataset.motionPhase).toBe("exiting");
    fireEvent.pointerMove(document, { pointerId: 1, clientX: 900 });
    expect(separator.getAttribute("aria-valuenow")).toBe("380");
    const event = new Event("transitionend", { bubbles: true });
    Object.defineProperty(event, "propertyName", { value: "transform" });
    fireEvent(drawer, event);
    expect(screen.getByRole("button", { name: "展开预测结果" })).not.toBeNull();
  });
  it("退出保留并排槽位，关闭中重开不被旧完成回调隐藏", async () => {
    containerWidth = 1400;
    render(<DrawerHarness />);
    const trigger = screen.getByRole("button", { name: "运行预测" });
    fireEvent.click(trigger);
    const drawer = screen.getByRole("dialog", { name: "性质预测结果" });
    await waitFor(() => expect(drawer.dataset.motionPhase).toBe("open"));
    fireEvent.click(screen.getByRole("button", { name: "关闭性质预测结果" }));
    expect(drawer.dataset.motionPhase).toBe("exiting");
    expect(document.querySelector(".np-sw-drawer-layer")?.getAttribute("data-motion-present")).toBe("true");
    expect(screen.queryByRole("button", { name: "展开预测结果" })).toBeNull();
    fireEvent.click(trigger);
    await waitFor(() => expect(drawer.dataset.motionPhase).toBe("open"));
    expect(screen.getByRole("dialog", { name: "性质预测结果" })).toBe(drawer);
    expect(drawer.hasAttribute("inert")).toBe(false);
    expect(drawer.querySelector(".np-sw-drawer__body")?.hasAttribute("aria-live")).toBe(false);
  });

  it("覆盖模式循环焦点，Escape 关闭并恢复触发器焦点", async () => {
    render(<DrawerHarness />);
    const trigger = screen.getByRole("button", { name: "运行预测" });
    expect(document.querySelector(".np-sw-drawer")?.hasAttribute("inert")).toBe(true);
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "性质预测结果" });
    expect(dialog.hasAttribute("inert")).toBe(false);
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(trigger.hasAttribute("inert")).toBe(false);
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
    const reopen = screen.getByRole("button", { name: "展开预测结果" });
    fireEvent.click(reopen);
    const reopenedDialog = screen.getByRole("dialog", { name: "性质预测结果" });
    expect(reopenedDialog.hasAttribute("inert")).toBe(false);
    const reopenedClose = screen.getByRole("button", { name: "关闭性质预测结果" });
    await waitFor(() => expect(document.activeElement).toBe(reopenedClose));
    fireEvent.click(reopenedClose);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "展开预测结果" })));
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

    const close = screen.getByRole("button", { name: "关闭性质预测结果" });
    close.focus();
    fireEvent.click(close);
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("允许模块按实际内容宽度提高并排阈值", async () => {
    containerWidth = 1300;
    render(<DrawerHarness overlayContainerWidth={1360} />);
    const trigger = screen.getByRole("button", { name: "运行预测" });
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "性质预测结果" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.querySelector(".np-sw-drawer-layer")?.classList.contains("is-overlay")).toBe(true);
  });
});
