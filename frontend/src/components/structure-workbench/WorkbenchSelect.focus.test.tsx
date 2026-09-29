// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkbenchSelect } from "./WorkbenchSelect";

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => window.setTimeout(() => callback(performance.now()), 16));
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(id => window.clearTimeout(id));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function chooseBeforeExitFinishes(keyboard = false) {
  const changed = vi.fn();
  render(<>
    <WorkbenchSelect id="private-task" ariaLabel="我的任务" value="a"
      options={[{ value: "a", label: "任务 A" }, { value: "b", label: "任务 B" }]} onChange={changed} />
    <button type="button">账号菜单</button>
  </>);
  const trigger = screen.getByRole("combobox", { name: "我的任务" });
  fireEvent.click(trigger);
  act(() => vi.advanceTimersByTime(1000));
  const option = screen.getByRole("option", { name: "任务 B" });
  option.focus();
  if (keyboard) fireEvent.keyDown(option, { key: "Enter" });
  else fireEvent.click(option);
  expect(changed).toHaveBeenCalledWith("b");
  expect(document.getElementById("private-task-listbox")?.dataset.motionPhase).toBe("exiting");
  return { trigger, account: screen.getByRole("button", { name: "账号菜单" }) };
}

it("does not reclaim focus from the next account control when the previous select finishes exiting", () => {
  const { account } = chooseBeforeExitFinishes();
  account.focus();
  expect(document.activeElement).toBe(account);
  act(() => vi.advanceTimersByTime(1000));
  expect(document.getElementById("private-task-listbox")).toBeNull();
  expect(document.activeElement).toBe(account);
});

it("still returns keyboard focus to its trigger when no other control took focus", () => {
  const { trigger } = chooseBeforeExitFinishes(true);
  act(() => vi.advanceTimersByTime(1000));
  expect(document.getElementById("private-task-listbox")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});
