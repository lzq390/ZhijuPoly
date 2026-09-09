// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDrawerMode } from "./useDrawerMode";

let width = 1400;
let notify: ResizeObserverCallback;
const disconnect = vi.fn();
function Container({ threshold = 1280 }: { threshold?: number }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const mode = useDrawerMode(ref, { closest: ".workbench", inlineMinWidth: threshold });
  return <div className="workbench"><div ref={ref} data-testid="mode">{mode}</div></div>;
}
beforeEach(() => {
  width = 1400;
  disconnect.mockClear();
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({ width } as DOMRect));
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { notify = callback; }
    observe() {}
    disconnect = disconnect;
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function resize(next: number) {
  act(() => notify([{ contentRect: { width: next } } as ResizeObserverEntry], {} as ResizeObserver));
}

describe("drawer layout mode", () => {
  it("uses available container width and retains the last mode for hidden kept-alive workspaces", () => {
    render(<Container />);
    expect(screen.getByTestId("mode").textContent).toBe("inline");
    resize(1160);
    expect(screen.getByTestId("mode").textContent).toBe("overlay");
    resize(0);
    expect(screen.getByTestId("mode").textContent).toBe("overlay");
    resize(2176);
    expect(screen.getByTestId("mode").textContent).toBe("inline");
  });
  it("keeps module-specific and 2K thresholds and cleans up observers", () => {
    const view = render(<Container threshold={2050} />);
    expect(screen.getByTestId("mode").textContent).toBe("overlay");
    resize(2050);
    expect(screen.getByTestId("mode").textContent).toBe("inline");
    view.rerender(<Container threshold={2200} />);
    expect(screen.getByTestId("mode").textContent).toBe("overlay");
    view.unmount();
    expect(disconnect).toHaveBeenCalledTimes(2);
  });
  it("still responds to window resizing without ResizeObserver", () => {
    vi.stubGlobal("ResizeObserver", undefined);
    render(<Container />);
    width = 900;
    act(() => window.dispatchEvent(new Event("resize")));
    expect(screen.getByTestId("mode").textContent).toBe("overlay");
  });
});
