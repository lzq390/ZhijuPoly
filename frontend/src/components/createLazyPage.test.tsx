// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef, forwardRef, useEffect, useImperativeHandle, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createLazyPage } from "./createLazyPage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("page transport and ownership", () => {
  it("deduplicates prefetch without mounting, and preserves the real component's ref and state", async () => {
    const mounted = vi.fn();
    const View = forwardRef<{ read: () => number }, { label: string }>(function View({ label }, ref) {
      const [count, setCount] = useState(0);
      useEffect(() => { mounted(); }, []);
      useImperativeHandle(ref, () => ({ read: () => count }), [count]);
      return <button onClick={() => setCount(n => n + 1)}>{label}:{count}</button>;
    });
    const load = vi.fn(async () => ({ default: View }));
    const resource = createLazyPage(load);
    expect(load).not.toHaveBeenCalled();
    const first = resource.preload();
    expect(resource.preload()).toBe(first);
    await first;
    expect(load).toHaveBeenCalledOnce();
    expect(mounted).not.toHaveBeenCalled();
    const ref = createRef<{ read: () => number }>();
    const view = render(<resource.Page label="first" ref={ref} />);
    fireEvent.click(screen.getByRole("button"));
    view.rerender(<resource.Page label="next" ref={ref} />);
    expect(ref.current?.read()).toBe(1);
    expect(screen.getByText("next:1")).toBeTruthy();
    expect(mounted).toHaveBeenCalledOnce();
  });

  it("keeps the shell usable while loading and never mounts a late page after navigation", async () => {
    let finish!: (value: { default: () => React.ReactNode }) => void;
    const mounted = vi.fn();
    const resource = createLazyPage(() => new Promise<{ default: () => React.ReactNode }>(resolve => { finish = resolve; }));
    const view = render(<><input aria-label="shell draft" defaultValue="preserve" /><resource.Page /></>);
    expect(screen.getByRole("status").textContent).toContain("页面加载中");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "still editing" } });
    await act(async () => {});
    view.rerender(<><input aria-label="shell draft" defaultValue="preserve" /><p>another page</p></>);
    await act(async () => finish({ default: () => { mounted(); return <p>late</p>; } }));
    expect(mounted).not.toHaveBeenCalled();
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("still editing");
    expect(screen.queryByText("late")).toBeNull();
  });

  it("retries failed transport without resetting the surrounding workspace", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const load = vi.fn<() => Promise<{ default: () => React.ReactNode }>>()
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValueOnce({ default: () => <p>loaded page</p> });
    const { Page } = createLazyPage(load);
    render(<><input aria-label="shared draft" defaultValue="CCO" /><Page /></>);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "CCN" } });
    const retry = await screen.findByRole("button", { name: "重试加载页面" });
    fireEvent.click(retry);
    expect(await screen.findByText("loaded page")).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(2);
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("CCN");
  });

  it("can prefetch again after an earlier failed navigation", async () => {
    const load = vi.fn<() => Promise<{ default: () => React.ReactNode }>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ default: () => <p>available</p> });
    const resource = createLazyPage(load);
    await expect(resource.preload()).rejects.toThrow("offline");
    await resource.preload();
    render(<resource.Page />);
    expect(screen.getByText("available")).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(2);
  });
});
