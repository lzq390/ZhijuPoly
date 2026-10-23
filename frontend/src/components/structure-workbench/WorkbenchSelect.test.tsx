// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { WorkbenchSelect } from "./WorkbenchSelect";
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const choices = [{ value: "a", label: "Alpha" }, { value: "b", label: "Blocked", disabled: true }, { value: "c", label: "Gamma" }];
function Select() { const [value, setValue] = useState("a"); return <WorkbenchSelect id="synthetic-choice" value={value} options={choices} onChange={setValue} ariaLabel="Synthetic choice" ariaDescribedBy="synthetic-help" ariaInvalid constrainToScrollParent />; }
describe("shared scientific selector", () => {
  it("forwards validation semantics and keeps keyboard selection past disabled choices", () => {
    render(<><p id="synthetic-help">Scientific explanation</p><Select /></>);
    const trigger = screen.getByRole("combobox", { name: "Synthetic choice" });
    expect(trigger.getAttribute("aria-describedby")).toBe("synthetic-help");
    expect(trigger.getAttribute("aria-invalid")).toBe("true");
    fireEvent.keyDown(trigger, { key: "ArrowDown" }); fireEvent.keyDown(trigger, { key: "ArrowDown" }); fireEvent.keyDown(trigger, { key: "Enter" });
    expect(trigger.textContent).toContain("Gamma"); expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });
  it("keeps an inline option menu within a clipping ancestor and updates on resize", () => {
    const { container } = render(<div data-testid="clip" style={{ overflowY: "auto" }}><Select /></div>);
    const clip = screen.getByTestId("clip");
    const trigger = screen.getByRole("combobox", { name: "Synthetic choice" });
    const rect = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 400, x: 0, y: top, width: 400, height: bottom - top, toJSON() {} });
    vi.spyOn(clip, "getBoundingClientRect").mockReturnValue(rect(20, 140));
    const height = vi.spyOn(clip, "clientHeight", "get").mockReturnValue(120);
    vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue(rect(80, 100));
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(280);
    fireEvent.click(trigger);
    const menu = screen.getByRole("listbox", { name: "Synthetic choice" });
    expect(container.querySelector(".np-sw-select")?.classList.contains("opens-up")).toBe(true);
    expect(menu.style.maxHeight).toBe("52px");
    height.mockReturnValue(480); fireEvent(window, new Event("resize"));
    expect(container.querySelector(".np-sw-select")?.classList.contains("opens-up")).toBe(false);
    expect(menu.style.maxHeight).toBe("310px");
  });
});


function finishOpacity(element: HTMLElement) {
  const event = new Event("transitionend", { bubbles: true });
  Object.defineProperty(event, "propertyName", { value: "opacity" });
  fireEvent(element, event);
}

it("keeps the reopened selector open and preserves option focus after a rapid reversal", () => {
  render(<Select />);
  const trigger = screen.getByRole("combobox", { name: "Synthetic choice" });
  fireEvent.click(trigger);
  fireEvent.keyDown(trigger, { key: "Escape" });
  fireEvent.click(trigger);
  const menu = screen.getByRole("listbox", { name: "Synthetic choice" });
  const option = within(menu).getByRole("option", { name: "Alpha" });
  option.focus(); finishOpacity(menu);
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("listbox", { name: "Synthetic choice" })).toBe(menu);
  expect(document.activeElement).toBe(option);
});

it("does not steal external focus when an exiting selector finishes", () => {
  render(<><Select /><button>Outside focus</button></>);
  const trigger = screen.getByRole("combobox", { name: "Synthetic choice" });
  fireEvent.click(trigger);
  const menu = screen.getByRole("listbox", { name: "Synthetic choice" });
  const option = within(menu).getByRole("option", { name: "Alpha" });
  option.focus();
  fireEvent.keyDown(option, { key: "Escape" });
  const outside = screen.getByRole("button", { name: "Outside focus" });
  outside.focus(); finishOpacity(menu);
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(outside);
});
