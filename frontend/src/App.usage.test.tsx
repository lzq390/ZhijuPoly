// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { preloadPage } from "./pages";
const mocks = vi.hoisted(() => ({ save: vi.fn() }));
vi.mock("./auth/AuthProvider", () => ({
  useAuth: () => null,
  // Real AccountMenu visibility/callback semantics are covered by its own suite.
  AccountMenu: ({ onOpenUsage }: { onOpenUsage?: () => void }) => <button onClick={onOpenUsage}>测试账号用量入口</button>
}));
vi.mock("./components/PersonalUsagePage", () => ({ PersonalUsagePage: () => <section><h1>个人用量测试页</h1></section> }));
vi.mock("./components/StructureWorkbenchPage", async () => {
  const { forwardRef, useImperativeHandle } = await import("react");
  return { StructureWorkbenchPage: forwardRef(function Workbench(_props, ref) {
    useImperativeHandle(ref, () => ({ syncBeforeLeave: (signal: AbortSignal) => mocks.save(signal) }));
    return <section><h1>用量导航前的画板</h1></section>;
  }) };
});
beforeAll(async () => { await Promise.all([preloadPage("structureWorkbench"), preloadPage("personalUsage")]); });
beforeEach(() => {
  window.history.replaceState({}, "", "/structure-workbench");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  mocks.save.mockReset();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("waits for the existing canvas guard before account-menu navigation commits", async () => {
  let release!: (result: { status: string }) => void;
  mocks.save.mockImplementation(() => new Promise(resolve => { release = resolve; }));
  render(<App />);
  await screen.findByRole("heading", { name: "用量导航前的画板" });
  fireEvent.click(screen.getByRole("button", { name: "测试账号用量入口" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledOnce());
  expect(window.location.pathname).toBe("/structure-workbench");
  expect(screen.queryByRole("heading", { name: "个人用量测试页" })).toBeNull();
  await act(async () => { release({ status: "saved" }); });
  await waitFor(() => expect(window.location.pathname).toBe("/account/usage"));
  await screen.findByRole("heading", { name: "个人用量测试页" });
  expect(mocks.save).toHaveBeenCalledOnce();
});
