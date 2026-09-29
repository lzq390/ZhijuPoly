// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { KnowledgeDetailDrawer } from "./KnowledgeDetailDrawer";

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => window.setTimeout(() => callback(performance.now()), 16));
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(id => window.clearTimeout(id));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function Harness() {
  const [open, setOpen] = useState(false);
  return <div className="ks-panel-layout">
    <button onClick={() => setOpen(true)}>历史记录</button>
    <KnowledgeDetailDrawer id="private-history" open={open} width={380} contentKey="history" title="在线历史"
      widthProfile={{ min: 320, max: 560, defaultWidth: 380, keyboardStep: 10, keyboardLargeStep: 40 }}
      onWidthChange={() => {}} onClose={() => setOpen(false)} onOpen={() => setOpen(true)}>
      <button>历史详情</button>
    </KnowledgeDetailDrawer>
    <button>账号菜单</button>
  </div>;
}

function closeDrawer() {
  render(<Harness />);
  const trigger = screen.getByRole("button", { name: "历史记录" });
  trigger.focus();
  fireEvent.click(trigger);
  act(() => vi.advanceTimersByTime(1000));
  const close = screen.getByRole("button", { name: "关闭详情" });
  expect(document.activeElement).toBe(close);
  fireEvent.keyDown(close, { key: "Escape" });
  expect(document.getElementById("private-history")?.dataset.motionPhase).toBe("exiting");
  return { trigger, account: screen.getByRole("button", { name: "账号菜单" }) };
}

it("keeps new account focus when a previously closed history drawer finishes exiting", () => {
  const { account } = closeDrawer();
  account.focus();
  act(() => vi.advanceTimersByTime(1000));
  act(() => vi.advanceTimersByTime(20));
  expect(document.getElementById("private-history")?.dataset.motionPhase).toBe("closed");
  expect(document.activeElement).toBe(account);
});

it("still returns focus to the history trigger after keyboard close", () => {
  const { trigger } = closeDrawer();
  act(() => vi.advanceTimersByTime(1000));
  act(() => vi.advanceTimersByTime(20));
  expect(document.activeElement).toBe(trigger);
});

it("checks the new focus when the already scheduled restore frame executes", () => {
  const { account } = closeDrawer();
  const drawer = document.getElementById("private-history")!;
  const transition = new Event("transitionend", { bubbles: true });
  Object.defineProperty(transition, "propertyName", { value: "transform" });
  fireEvent(drawer, transition);
  expect(drawer.dataset.motionPhase).toBe("closed");
  account.focus();
  act(() => vi.advanceTimersByTime(20));
  expect(document.activeElement).toBe(account);
});

it("cancels the old exit restore when the drawer reopens", () => {
  const { trigger } = closeDrawer();
  fireEvent.click(trigger);
  act(() => vi.advanceTimersByTime(1000));
  act(() => vi.advanceTimersByTime(20));
  expect(document.getElementById("private-history")?.dataset.motionPhase).toBe("open");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭详情" }));
});
