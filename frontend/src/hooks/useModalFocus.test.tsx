// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ModalFocusSuspendedContext, useModalFocus } from "./useModalFocus";

function Harness({ open = true, global = false }: { open?: boolean; global?: boolean }) {
  const scopeRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const modalFocusActive = useModalFocus({ active: open, open, scopeRef, panelRef, onClose: () => {}, global });
  return <>
    <button>平台导航</button>
    <div data-testid="module">
      <div ref={scopeRef}>
        <div ref={panelRef} role="dialog" aria-modal={modalFocusActive} tabIndex={-1}>
          <button style={{ visibility: "hidden" }}>关闭结果</button>
        </div>
      </div>
    </div>
  </>;
}

async function paintedFrames(count = 5) {
  await act(async () => {
    for (let index = 0; index < count; index++) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    }
  });
}

afterEach(cleanup);

describe("useModalFocus initial visibility", () => {
  it("suspends inert and focus trapping beneath a foreground panel, then restores the drawer", async () => {
    const tree = (suspended: boolean) => <ModalFocusSuspendedContext.Provider value={suspended}><Harness global /></ModalFocusSuspendedContext.Provider>;
    const view = render(tree(false));
    const navigation = screen.getByText("平台导航");
    const close = screen.getByText("关闭结果");
    close.style.visibility = "visible";
    await waitFor(() => expect(document.activeElement).toBe(close));
    expect(navigation.hasAttribute("inert")).toBe(true);
    view.rerender(tree(true));
    expect(navigation.hasAttribute("inert")).toBe(false);
    expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("false");
    navigation.focus();
    await paintedFrames();
    expect(document.activeElement).toBe(navigation);
    view.rerender(tree(false));
    await waitFor(() => expect(document.activeElement).toBe(close));
    expect(navigation.hasAttribute("inert")).toBe(true);
  });
  it("waits for the opening control to become visible instead of dropping its initial focus", async () => {
    render(<Harness />);
    const navigation = screen.getByText("平台导航");
    navigation.focus();
    await paintedFrames();
    expect(document.activeElement).toBe(navigation);

    const close = screen.getByText("关闭结果");
    close.style.visibility = "visible";
    await waitFor(() => expect(document.activeElement).toBe(close));
    expect(navigation.hasAttribute("inert")).toBe(false);
  });

  it("cancels a pending focus when the drawer closes before it is painted", async () => {
    const view = render(<Harness />);
    await paintedFrames();
    view.rerender(<Harness open={false} />);
    const navigation = screen.getByText("平台导航");
    navigation.focus();
    screen.getByText("关闭结果").style.visibility = "visible";
    await paintedFrames();
    expect(document.activeElement).toBe(navigation);
  });

  it("does not steal focus after a retained module starts switching away", async () => {
    render(<Harness />);
    await paintedFrames();
    screen.getByTestId("module").dataset.moduleTransitioning = "true";
    const navigation = screen.getByText("平台导航");
    navigation.focus();
    screen.getByText("关闭结果").style.visibility = "visible";
    await paintedFrames();
    expect(document.activeElement).toBe(navigation);
    expect(navigation.hasAttribute("inert")).toBe(false);
  });

  it("cancels the pending frame on unmount", async () => {
    const view = render(<Harness />);
    await paintedFrames();
    view.unmount();
    const next = document.createElement("button");
    document.body.append(next);
    next.focus();
    await paintedFrames();
    expect(document.activeElement).toBe(next);
    next.remove();
  });
});
