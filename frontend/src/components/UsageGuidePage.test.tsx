// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import guide from "../help/usage-guide.json";
import { UsageGuidePage } from "./UsageGuidePage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function expandFirst(container: HTMLElement) {
  const details = container.querySelector<HTMLDetailsElement>(".np-guide-screenshots")!;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(details.querySelector("img")).not.toBeNull());
  return details;
}

describe("UsageGuidePage", () => {
  it("does not mount screenshot images until that section is expanded", async () => {
    const { container } = render(<UsageGuidePage onOpenModule={vi.fn()} />);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.querySelector<HTMLDetailsElement>(".np-guide-mobile-contents")!.open).toBe(false);
    const details = await expandFirst(container);
    const image = details.querySelector("img")!;
    expect(image.getAttribute("src")).toBe(guide.sections[0].screenshots[0].src);
    expect(image.getAttribute("loading")).toBe("lazy");
    expect(container.querySelectorAll("img")).toHaveLength(guide.sections[0].screenshots.length);
    details.open = false;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(container.querySelectorAll("img")).toHaveLength(0));
  });

  it("enlarges screenshots, traps modal focus and restores focus when Escape closes it", async () => {
    const { container } = render(<UsageGuidePage onOpenModule={vi.fn()} />);
    const details = await expandFirst(container);
    const trigger = within(details).getAllByRole("button", { name: /^放大截图：/ })[0];
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "操作截图" });
    const close = within(dialog).getByRole("button", { name: "关闭截图" });
    await waitFor(() => expect(container.hasAttribute("inert")).toBe(true));
    close.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(container.hasAttribute("inert")).toBe(false);
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe("");
  });

  it("offers a retry if an independent screenshot cannot load", async () => {
    const { container } = render(<UsageGuidePage onOpenModule={vi.fn()} />);
    const details = await expandFirst(container);
    fireEvent.error(details.querySelector("img")!);
    expect(within(details).getByText("截图暂时无法显示。")).toBeTruthy();
    fireEvent.click(within(details).getByRole("button", { name: "重新加载截图" }));
    expect(details.querySelector("img")).not.toBeNull();
  });

  it("uses module callbacks for functional navigation, including conversations", () => {
    const onOpenModule = vi.fn();
    const { container } = render(<UsageGuidePage onOpenModule={onOpenModule} />);
    const structure = container.querySelector('[aria-labelledby="usage-guide-structureWorkbench"]')!;
    fireEvent.click(within(structure as HTMLElement).getByRole("button", { name: "打开功能" }));
    const home = container.querySelector('[aria-labelledby="usage-guide-home"]')!;
    fireEvent.click(within(home as HTMLElement).getByRole("button", { name: "打开功能" }));
    expect(onOpenModule.mock.calls).toEqual([["structureWorkbench"], ["home"]]);
    expect(container.querySelector('a[target="_blank"]')).toBeNull();
  });

  it("jumps within the guide scroll area without scrolling the application shell", () => {
    const scrollWindow = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const { container } = render(<UsageGuidePage onOpenModule={vi.fn()} />);
    const region = container.querySelector<HTMLElement>(".np-guide-scroll")!;
    region.scrollTop = 10;
    const heading = document.getElementById("usage-guide-knowledge")!;
    vi.spyOn(region, "getBoundingClientRect").mockReturnValue({ top: 100 } as DOMRect);
    vi.spyOn(heading, "getBoundingClientRect").mockReturnValue({ top: 500 } as DOMRect);
    fireEvent.click(within(screen.getByRole("complementary", { name: "功能目录" })).getByRole("button", { name: /知识检索/ }));
    expect(region.scrollTop).toBe(390);
    expect(scrollWindow).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(heading);
  });
});
