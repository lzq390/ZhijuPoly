// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { retireSession } from "../auth/session";
import { useStructureWorkspace } from "./useStructureWorkspace";

const standardize = vi.hoisted(() => vi.fn());
vi.mock("../services/api", () => ({ standardizeSmiles: standardize }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("does not hand account A's late local snapshot to a caller that can submit under account B", async () => {
  const { result } = renderHook(useStructureWorkspace);
  let complete!: (smiles: string) => void;
  vi.spyOn(result.current.workspace, "getCurrentSmiles").mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
  const pending = result.current.getCurrentSmiles();
  retireSession();
  complete("account-a-private-input");
  await expect(pending).resolves.toBe("");
  await expect(result.current.getCurrentSmiles()).resolves.toBe("");
});

it("refuses deferred server standardization from an old workspace", async () => {
  const { result } = renderHook(useStructureWorkspace);
  retireSession();
  await act(async () => { result.current.workspace.setSmiles("account-a-input"); });
  expect(standardize).not.toHaveBeenCalled();
});


it("keeps guest drawing and text conversion local", async () => {
  const { result } = renderHook(() => useStructureWorkspace({ guest: true, initialSmiles: "CCO" }));
  await act(async () => { result.current.workspace.setSmiles("CCN"); });
  await act(async () => { expect(await result.current.getCurrentSmiles()).toBe("CCN"); });
  expect(standardize).not.toHaveBeenCalled();
});
