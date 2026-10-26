// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MdStructureInput } from "./MdStructureInput";
const auth = vi.hoisted(() => ({ guest: false }));
const api = vi.hoisted(() => ({ preview: vi.fn() }));
vi.mock("../../auth/AuthProvider", () => ({ useAuth: () => ({ status: auth.guest ? "guest" : "authenticated" }) }));
vi.mock("../../services/api", () => ({ fetchStructure2D: api.preview }));
vi.mock("../../structure-library/StructureLibraryProvider", () => ({ useStructureImport: () => ({ choose: async () => {}, source: null }) }));
const props = { value: "*CC*", error: null, onChange: vi.fn(), onTouched: vi.fn(), onEditStructure: vi.fn() };
beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); auth.guest = false; api.preview.mockResolvedValue({ structure_svg: '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1"/></svg>' }); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
const tick = async () => { await act(async () => { vi.advanceTimersByTime(500); await Promise.resolve(); }); };

it("does not generate a guest preview", async () => {
  auth.guest = true; render(<MdStructureInput {...props} />); await tick();
  expect(api.preview).not.toHaveBeenCalled();
  expect(screen.getByText("登录后生成 2D 预览")).toBeTruthy();
});

it("aborts the old preview on guest transition and reloads the same value for an authenticated identity", async () => {
  let resolve!: (value: { structure_svg: string }) => void;
  const pending = new Promise<{ structure_svg: string }>(next => { resolve = next; });
  let signal!: AbortSignal;
  api.preview.mockImplementationOnce((_smiles: string, current: AbortSignal) => { signal = current; return pending; });
  const view = render(<MdStructureInput {...props} />); await tick();
  expect(api.preview).toHaveBeenCalledOnce();
  auth.guest = true; view.rerender(<MdStructureInput {...props} />);
  expect(signal.aborted).toBe(true);
  await act(async () => { resolve({ structure_svg: '<svg xmlns="http://www.w3.org/2000/svg"/>' }); await pending; });
  expect(screen.getByText("登录后生成 2D 预览")).toBeTruthy();
  auth.guest = false; view.rerender(<MdStructureInput {...props} />); await tick();
  expect(api.preview).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("登录后生成 2D 预览")).toBeNull();
});
